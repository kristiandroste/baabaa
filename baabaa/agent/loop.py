"""The agent loop. A turn is the model's whole reply to one user message: thinking, text, and tool calls
with their results, repeated until the model answers without calling a tool.

Live updates go to every open window of the account (browser or terminal) through the event bus.
Stop cancels the turn's task: the model stream closes (freeing the GPU queue at once), running
commands are killed, and the partial reply is kept.
"""

import asyncio
import json
import os
import re

from ..gateway import ModelNotAllowed, ResidencyError
from ..ollama import OllamaError
from ..util import clip, mono_ms, new_id, now_ms
from . import checkpoints, context, judge, permissions, prompts
from . import tools as T
from .permissions import Decision

MAX_STEPS = 60
JUDGE_MIN_PARAMS_B = 3.0
DELTA_INTERVAL_MS = 40
PARSE_RETRIES = 2      # times a turn asks the model again after Ollama could not read its tool call

# When a step goes wrong in a way small models are prone to, baabaa tells the person (a notice) and sends the
# model back once with a note (each kind at most once per turn; an unreadable tool call up to PARSE_RETRIES).
RECOVERY = {
    "parse_error": (
        "The model's tool call came out malformed (a known problem in Ollama with long tool calls), so baabaa asked it again.",
        "Your last tool call could not be read, so it did not run (Ollama said: {error}). If you were writing a web "
        "page or another long piece of content, write it in your reply instead, as one complete fenced block such as "
        "```html … ```; baabaa shows a page written that way as an artifact. Otherwise make the tool call again, carefully."),
    "text_call": (
        "The model wrote a tool call as text; asking it to use a real tool call.",
        "Your last message wrote a tool call as plain text, so it did not run and there are no results from it. "
        "Call the tool with a proper tool call instead, and never describe results of a call that has not run."),
    "empty_reply": (
        "The model stopped without replying, so baabaa asked it to continue.",
        "You stopped without writing a reply to the user. If you meant to use a tool, call it now; otherwise write your answer."),
    "unfinished_claim": (
        "The reply said the work was done, but no tool ran, so baabaa asked the model to do it.",
        "Your reply says something was made or changed, but you made no tool call in this reply, so nothing was made "
        "or changed. Do it now with the right tool call (for an artifact: create_artifact, update_artifact or "
        "rewrite_artifact; for a file: write_file or edit_file), or tell the user plainly that it is not done. If you "
        "meant work from an earlier reply, say exactly what was done then and what was not."),
}
# Tools whose success means something was made or changed in this turn (see _unfinished)
CHANGE_TOOLS = {"create_artifact", "update_artifact", "rewrite_artifact", "write_file", "edit_file", "create_file",
                "bash", "generate_image", "memory", "todo_write"}


class HookBlocked(Exception):
    pass


class Turn:
    def __init__(self, conv_id: str, account: dict, window: str):
        self.conv_id = conv_id
        self.account = account
        self.window = window
        self.task: asyncio.Task | None = None
        self.state = "running"
        self.queue: list[tuple[list, str]] = []
        self.stopped = False
        self.assistant_id = None
        self.user_msg_id = None
        self.checkpointed = False
        self.started = mono_ms()
        self.blocks: list[dict] = []
        # knowledge for this turn (set by Agent._prepare_knowledge)
        self.project: dict | None = None
        self.project_prompt = ""
        self.project_mode = "none"
        self.memory_on = False
        self.memory_prompt = ""
        self.past_chats_on = False

    def public(self) -> dict:
        return {"conv_id": self.conv_id, "state": self.state, "assistant_id": self.assistant_id,
                "queued": [clip(" ".join(b.get("text", "") for b in blocks if b.get("type") == "text"), 200)
                           for blocks, _ in self.queue]}


class Agent:
    def __init__(self, app):
        self.app = app
        self.turns: dict[str, Turn] = {}
        self.pending: dict[str, dict] = {}   # approvals and questions waiting for a person
        self._last_delta: dict[str, float] = {}
        self._pd: dict[str, dict] = {}

    # events ---------------------------------------------------------------------------------------
    def _pub(self, account_id: str, type: str, data: dict) -> None:
        self.app.events.publish(account_id, type, data)

    def _turn_state(self, turn: Turn) -> None:
        data = turn.public()
        data["queue_position"] = self.app.gateway.queue.position(turn.conv_id)
        self._pub(turn.account["id"], "turn", data)

    def live_blocks(self, conv_id: str) -> tuple[str | None, list]:
        """The running turn's assistant message id and its blocks so far (for a window that just opened)."""
        turn = self.turns.get(conv_id)
        if not turn or not turn.assistant_id:
            return None, []
        self._flush(turn, turn.blocks)
        return turn.assistant_id, [dict(b) for b in turn.blocks]

    def state(self, conv_id: str) -> dict | None:
        turn = self.turns.get(conv_id)
        if not turn:
            return None
        data = turn.public()
        data["queue_position"] = self.app.gateway.queue.position(conv_id)
        data["pending"] = [p["payload"] for p in self.pending.values() if p["conv_id"] == conv_id]
        return data

    # entry points ---------------------------------------------------------------------------------
    async def send(self, account: dict, conv_id: str, text: str, attachment_ids: list, window: str,
                   parent_id: str | None = None, edit: bool = False, research: bool = False,
                   image: dict | None = None) -> dict:
        self.app.restarter.refuse_new_work()
        store = self.app.stores.get(account["id"])
        conv = store.conversation(conv_id)
        if conv is None:
            raise KeyError(conv_id)
        blocks = [{"type": "text", "text": text}] if text.strip() else []
        if text.strip().startswith("/"):
            folder = conv["folder"]
            expanded = self.app.extend.expand_command(text, account["id"], folder, bool(folder) and self.app.maindb.is_trusted(folder))
            if expanded:
                blocks[0].update({"command": expanded[0], "expanded": expanded[1]})
                self.app.stats.event("command_used", account["id"], conv_id, window, command=expanded[0])
            elif text.strip().lower().startswith("/research ") and len(text.strip()) > 10:
                research = True  # the built-in command, from any window (a custom /research wins)
                blocks[0].update({"command": "research", "expanded": text.strip()[10:].strip()})
        if research and blocks:
            blocks[0]["research"] = True
        if image is not None and blocks:  # the composer's Image mode: {shape, edit}
            blocks[0].update({"image": True, "shape": image.get("shape") or "square", "edit": bool(image.get("edit"))})
        for aid in attachment_ids or []:
            att = store.attachment(aid)
            if att:
                blocks.append({"type": "attachment", "id": att["id"], "name": att["name"], "mime": att["mime"],
                               "kind": att["kind"], "size": att["size"]})
        if not blocks:
            raise ValueError("empty message")
        turn = self.turns.get(conv_id)
        if turn and not edit:
            turn.queue.append((blocks, window))
            self._turn_state(turn)
            return {"queued": True}
        if turn and edit:
            self.stop(conv_id)
            await asyncio.sleep(0)
        parent = parent_id if (parent_id is not None or edit) else conv["leaf_id"]
        user = store.add_message(conv_id, parent, "user", blocks)
        self._pub(account["id"], "msg.new", {"conv_id": conv_id, "message": user})
        self.app.stats.event("message", account["id"], conv_id, window, edit=edit, attachments=len(attachment_ids or []),
                             chars=len(text))
        self._start(account, conv_id, user["id"], window)
        return {"message": user}

    def regenerate(self, account: dict, conv_id: str, assistant_id: str, window: str) -> None:
        self.app.restarter.refuse_new_work()
        store = self.app.stores.get(account["id"])
        m = store.message(assistant_id)
        if m is None or m["conv_id"] != conv_id or m["role"] != "assistant":
            raise KeyError(assistant_id)
        if conv_id in self.turns:
            raise RuntimeError("A reply is already being written")
        self.app.stats.event("regenerate", account["id"], conv_id, window)
        self._start(account, conv_id, m["parent_id"], window)

    def _start(self, account: dict, conv_id: str, user_msg_id: str, window: str) -> None:
        turn = Turn(conv_id, account, window)
        turn.user_msg_id = user_msg_id
        self.turns[conv_id] = turn
        turn.task = asyncio.get_running_loop().create_task(self._run(turn))

    def stop(self, conv_id: str) -> bool:
        turn = self.turns.get(conv_id)
        if not turn:
            return False
        turn.stopped = True
        turn.queue.clear()
        for key, p in list(self.pending.items()):
            if p["conv_id"] == conv_id and not p["future"].done():
                p["future"].cancel()
        if turn.task:
            turn.task.cancel()
        self.app.shells.kill_all(conv_id)
        return True

    def resolve(self, pending_id: str, account: dict, answer: dict) -> None:
        p = self.pending.get(pending_id)
        if p is None:
            raise KeyError(pending_id)
        if account["id"] not in p["answerers"]:
            raise PermissionError("This request is for another account")
        if not p["future"].done():
            p["future"].set_result({**answer, "by": account["id"]})

    # the turn -------------------------------------------------------------------------------------
    async def _run(self, turn: Turn) -> None:
        account = turn.account
        store = self.app.stores.get(account["id"])
        conv = store.conversation(turn.conv_id)
        registry = self.app.registry
        model = conv["model"] if conv["model"] in [m["name"] for m in registry.approved("chat")] else registry.default_chat()
        blocks: list[dict] = turn.blocks
        meta = {"output_tokens": 0}
        assistant = store.add_message(turn.conv_id, turn.user_msg_id, "assistant", blocks, model=model, status="streaming")
        turn.assistant_id = assistant["id"]
        self._pub(account["id"], "msg.new", {"conv_id": turn.conv_id, "message": assistant})
        self._turn_state(turn)
        status = "ok"
        try:
            user0 = store.message(turn.user_msg_id)
            image_only = any(b.get("image") for b in user0["blocks"] if b.get("type") == "text")
            if not model and not image_only:
                raise ModelNotAllowed("No model is available yet. The owner needs to approve one in Settings > Models.")
            if model and model != conv["model"]:
                store.update_conversation(turn.conv_id, model=model)
            user = store.message(turn.user_msg_id)
            prompt = " ".join(b.get("expanded") or b.get("text", "") for b in user["blocks"] if b.get("type") == "text")
            for res in await self._hooks(turn, conv, "UserPromptSubmit", {"prompt": prompt}):
                if res["code"] == 2:
                    raise HookBlocked(res["stderr"].strip() or "A hook blocked this message.")
                if res["code"] == 0 and res["stdout"].strip():
                    extra = {"type": "hook_context", "text": clip(res["stdout"].strip(), 8000)}
                    store.update_message(turn.user_msg_id, blocks=user["blocks"] + [extra])
            first = next((b for b in user["blocks"] if b.get("type") == "text"), {})
            if first.get("image"):
                has_img = any(b.get("type") == "attachment" and b.get("kind") == "image" for b in user["blocks"])
                await self._make_image(turn, store, blocks, prompt, first.get("shape") or "square",
                                       edit=bool(first.get("edit")) or has_img)
            elif first.get("research"):
                from . import research
                caps = (registry.get(model) or {}).get("info", {}).get("capabilities") or []
                await research.run(self, turn, store, model, caps, blocks, prompt)
            else:
                await self._prepare_knowledge(turn, store, model, blocks)
                await self._prepare_scratch(turn, store)
                await self._loop(turn, store, model, blocks, meta)
        except asyncio.CancelledError:
            status = "stopped"
        except (OllamaError, ResidencyError, ModelNotAllowed, HookBlocked) as exc:
            blocks.append({"type": "error", "text": str(exc)})
            status = "error"
        except Exception as exc:  # keep the server alive and tell the user
            import traceback
            traceback.print_exc()
            blocks.append({"type": "error", "text": f"Internal error: {exc!r}"})
            status = "error"
        finally:
            for b in blocks:
                if b.get("type") == "tool" and b.get("status") in ("pending", "running", "waiting"):
                    b["status"] = "stopped"
                    b["output"] = b.get("output") or "Stopped."
            meta["duration_ms"] = round(mono_ms() - turn.started)
            store.update_message(turn.assistant_id, blocks=blocks, status=status, meta=meta, model=model)
            store.touch(turn.conv_id)
            self._pub(account["id"], "msg.done", {"conv_id": turn.conv_id, "message": store.message(turn.assistant_id)})
            self.app.stats.event("turn", account["id"], turn.conv_id, turn.window, status=status,
                                 tools=sum(1 for b in blocks if b.get("type") == "tool"), duration_ms=meta["duration_ms"],
                                 model=model)
            if self.turns.get(turn.conv_id) is turn:
                self.turns.pop(turn.conv_id, None)
                self._pub(account["id"], "turn", {"conv_id": turn.conv_id, "state": "idle"})
            queued = list(turn.queue)
            if status == "ok" and not store.conversation(turn.conv_id)["title"]:
                first = next((b for b in store.message(turn.user_msg_id)["blocks"] if b.get("type") == "text"), {})
                if first.get("image"):  # no model call just to name an image
                    words = " ".join((first.get("text") or "Image").split()[:7])
                    store.update_conversation(turn.conv_id, title=clip(words, 80))
                    self._pub(account["id"], "conv", {"conversation": store.conversation(turn.conv_id)})
                elif model:
                    asyncio.get_running_loop().create_task(self._title(account, turn.conv_id, model))
            if queued and not turn.stopped:
                texts, atts, window = [], [], queued[-1][1]
                for qblocks, _ in queued:
                    texts += [b["text"] for b in qblocks if b.get("type") == "text"]
                    atts += [b for b in qblocks if b.get("type") == "attachment"]
                qb = ([{"type": "text", "text": "\n\n".join(texts)}] if texts else []) + atts
                user = store.add_message(turn.conv_id, turn.assistant_id, "user", qb)
                self._pub(account["id"], "msg.new", {"conv_id": turn.conv_id, "message": user})
                self._start(account, turn.conv_id, user["id"], window)

    async def _loop(self, turn: Turn, store, model: str, blocks: list, meta: dict) -> None:
        registry, gateway = self.app.registry, self.app.gateway
        info = registry.get(model)
        caps = info["info"].get("capabilities") or []
        num_ctx = registry.num_ctx(model)
        nudge, stop_rounds, unreadable = None, 0, 0
        checked: set[str] = set()   # the checks that already sent the model back this turn
        for step in range(MAX_STEPS):
            conv = store.conversation(turn.conv_id)
            folder = conv["folder"]
            workdir, wkind = self._workdir(turn, conv)
            trusted = bool(folder) and self.app.maindb.is_trusted(folder)
            skills = list(self.app.extend.skills(turn.account["id"], folder, trusted).values())
            agents = self.app.extend.agents(turn.account["id"], folder, trusted)
            schemas, tool_list = [], []
            if "tools" in caps:
                mcp_tools = await self._mcp_tools(turn.account["id"])
                few = len(mcp_tools) <= 5
                tool_list = T.available(bool(workdir), conv["mode"], connectors=bool(mcp_tools) and not few,
                                        skills=bool(skills), scratch=wkind == "scratch", **self._knowledge_tools(turn))
                schemas = T.schemas(tool_list, scratch=wkind == "scratch")
                if agents:
                    for s in schemas:
                        fn = s["function"]
                        if fn["name"] == "task":
                            fn["parameters"] = json.loads(json.dumps(fn["parameters"]))
                            names = ["explore", "general"] + sorted(agents)
                            fn["parameters"]["properties"]["agent"] = {"type": "string", "enum": names, "description":
                                "explore (read-only search), general, or: " + "; ".join(
                                    f"{n}: {clip(a['description'], 120)}" for n, a in sorted(agents.items()))}
                active = set(conv["settings"].get("active_tools") or [])
                for mt in mcp_tools:
                    if (few or mt["id"] in active) and (conv["mode"] != "plan" or not folder or mt["read_only"]):
                        schemas.append({"type": "function", "function": {
                            "name": mt["id"], "description": f"[{mt['server']}] {mt['description']}",
                            "parameters": mt["schema"]}})
            system = prompts.system_prompt(
                model, folder, conv["mode"], bool(conv["settings"].get("network")),
                instructions=turn.account["settings"].get("instructions", ""),
                folder_notes=context.folder_notes(folder) if trusted else "",
                tools=bool(schemas), skills=skills, style=self._style(turn, conv, folder, trusted),
                loaded_skills=self._routed_skills(turn, store, skills), project=turn.project_prompt,
                memory=turn.memory_prompt, scratch=workdir if wkind == "scratch" and schemas else None)
            messages = await self._fit(turn, store, conv, model, num_ctx, system, schemas, blocks, caps)
            if nudge:
                messages.append({"role": "user", "content": nudge})
                nudge = None
            think = self._think(conv, caps)
            text_block = think_block = None
            calls, final = [], {}
            try:
                async for chunk in gateway.chat(model=model, messages=messages, tools=schemas or None, think=think,
                                                account_id=turn.account["id"], conv_id=turn.conv_id,
                                                msg_id=turn.assistant_id, window=turn.window):
                    msg = chunk.get("message") or {}
                    if msg.get("thinking"):
                        if think_block is None:
                            think_block = {"type": "thinking", "text": ""}
                            blocks.append(think_block)
                            self._block(turn, blocks, think_block)
                        think_block["text"] += msg["thinking"]
                        self._delta(turn, blocks, think_block, msg["thinking"])
                    if msg.get("content"):
                        if text_block is None:
                            text_block = {"type": "text", "text": ""}
                            blocks.append(text_block)
                            self._block(turn, blocks, text_block)
                        text_block["text"] += msg["content"]
                        self._delta(turn, blocks, text_block, msg["content"])
                    if msg.get("tool_calls"):
                        calls.extend(msg["tool_calls"])
                    if chunk.get("done"):
                        final = chunk
            except OllamaError as exc:
                # Ollama could not read the model's tool call and ended the reply (long tool calls with web
                # pages trip its strict parser): ask again, then give up with a plain message.
                if not tool_call_unreadable(str(exc)):
                    raise
                self._flush(turn, blocks)
                if unreadable >= PARSE_RETRIES:
                    raise OllamaError(
                        f"The model's tool call came out malformed {unreadable + 1} times in a row, so it could not run "
                        f"(Ollama could not read it: {clip(str(exc), 160)}). Try again, ask for a smaller piece at a "
                        "time, or switch to a larger model.") from exc
                unreadable += 1
                nudge = self._recovery(turn, blocks, model, "parse_error", error=clip(str(exc), 200))
                continue
            self._flush(turn, blocks)
            self._account_tokens(turn, store, messages, schemas, final, meta, num_ctx)
            if final.get("done_reason") == "length":
                blocks.append({"type": "notice", "text": "The reply reached the length limit."})
            elif text_block is not None and not any(_function(c) in ARTIFACT_TOOLS for c in calls):
                # after an artifact tool ran in this turn, a page in the reply is a copy: point to it, save nothing
                made = any(b.get("type") == "tool" and b.get("name") in ARTIFACT_TOOLS and b.get("status") == "done"
                           for b in blocks)
                self._promote_pages(turn, store, blocks, text_block, save=not made)
            if not calls and stop_rounds < 2:
                results = await self._hooks(turn, conv, "Stop", {"reply": (text_block or {}).get("text", "")})
                blocking = next((r for r in results if r["code"] == 2), None)
                if blocking:
                    stop_rounds += 1
                    nudge = "A check on your work says: " + (blocking["stderr"].strip() or "the task is not finished") + " Continue."
                    notice = {"type": "notice", "text": "A stop hook asked the model to continue."}
                    blocks.append(notice)
                    self._block(turn, blocks, notice)
                    continue
            if not calls:
                kind = self._unfinished(turn, store, blocks, (text_block or {}).get("text", ""), tool_list, checked)
                if kind:
                    checked.add(kind)
                    nudge = self._recovery(turn, blocks, model, kind)
                    continue
                return
            for call in calls:
                await self._tool(turn, store, model, caps, blocks, call)
            if turn.queue:
                return  # steering: the queued message starts the next turn with these results in context
            if _repeating_failures(blocks):
                notice = {"type": "notice", "text": "Stopped: the model kept repeating an action that fails. "
                          "Try rephrasing, or switch to a larger model."}
                blocks.append(notice)
                self._block(turn, blocks, notice)
                return
        blocks.append({"type": "notice", "text": f"Stopped after {MAX_STEPS} steps. Send a message to continue."})

    async def _hooks(self, turn, conv, event: str, payload: dict, tool: str | None = None) -> list[dict]:
        """Run the hooks for `event` (sandboxed, in order). Returns their results; empty when none apply."""
        from ..extend import hook_matches, run_hook
        folder = conv["folder"]
        trusted = bool(folder) and self.app.maindb.is_trusted(folder)
        hooks = self.app.extend.hooks(turn.account["id"], folder, trusted).get(event) or []
        if tool is not None:
            hooks = [h for h in hooks if hook_matches(h["matcher"], tool)]
        if not hooks:
            return []
        ctx = self._context(turn, conv)
        results = []
        for h in hooks:
            t0 = mono_ms()
            body = {"event": event, "conversation_id": turn.conv_id, "folder": folder, **payload}
            try:
                res = await run_hook(self.app.shells, turn.account["id"], h, body, folder, ctx.writable, ctx.readable)
            except OSError as exc:
                res = {"code": 1, "stdout": "", "stderr": str(exc)}
            res["command"] = h["command"]
            results.append(res)
            self.app.stats.event("hook", turn.account["id"], turn.conv_id, turn.window, hook_event=event, tool=tool,
                                 exit_code=res["code"], duration_ms=round(mono_ms() - t0))
            if res["code"] == 2:
                break
        return results

    def _routed_skills(self, turn, store, skills) -> list[str]:
        """Small models often skip use_skill, so a clearly matching skill is loaded for the turn."""
        if not skills:
            return []
        from ..extend import match_skills
        user = store.message(turn.user_msg_id)
        text = " ".join(b.get("expanded") or b.get("text", "") for b in user["blocks"] if b.get("type") == "text")
        hits = match_skills(skills, text)
        if hits and not getattr(turn, "_skill_logged", False):
            turn._skill_logged = True
            self.app.stats.event("skill_used", turn.account["id"], turn.conv_id, turn.window, skill=hits[0]["name"],
                                 source=hits[0]["source"], routed=True)
        return [self.app.extend.load_skill(s) for s in hits]

    def _style(self, turn, conv, folder, trusted) -> str:
        name = conv["settings"].get("style") or turn.account["settings"].get("style") or "default"
        s = self.app.extend.styles(turn.account["id"], folder, trusted).get(name)
        return s["body"] if s else ""

    def _think(self, conv: dict, caps: list):
        if "thinking" not in caps:
            return None
        level = conv["settings"].get("think", False)
        if level in (True, "on"):
            return True
        if level in ("low", "medium", "high"):
            return level
        return False

    async def _fit(self, turn, store, conv, model, num_ctx, system, schemas, blocks, caps) -> list:
        """Build the model's messages and make them fit: clear old tool output, then summarize."""
        vision = "vision" in caps
        cpt = float(conv["settings"].get("cpt") or context.DEFAULT_CHARS_PER_TOKEN)
        reserve = min(8192, num_ctx // 4)
        budget = num_ctx - reserve - context.tools_tokens(schemas, cpt)
        for attempt in range(2):
            thread = store.path(turn.user_msg_id)
            messages = context.build(thread, system, store, vision, blocks)
            messages, cleared = context.clear_old_tool_output(messages, int(budget * 0.85), cpt)
            est = context.estimate_tokens(messages, cpt)
            if est <= budget:
                if cleared:
                    self._pub(turn.account["id"], "context", {"conv_id": turn.conv_id, "cleared": cleared})
                return messages
            if attempt == 0:
                user = store.message(turn.user_msg_id)
                if user["parent_id"] is None:
                    break
                await self.compact(turn.account, turn.conv_id, before=turn.user_msg_id, model=model, window=turn.window,
                                   reason="auto")
        raise ModelNotAllowed(
            "This conversation no longer fits the model's context window, even after compaction. "
            "Start a new conversation, or switch to a model with a larger window.")

    def _account_tokens(self, turn, store, messages, schemas, final, meta, num_ctx) -> None:
        pt, ot = final.get("prompt_eval_count"), final.get("eval_count")
        if ot:
            meta["output_tokens"] = meta.get("output_tokens", 0) + ot
        if pt:
            meta["prompt_tokens"] = pt
            chars = sum(len(m.get("content") or "") for m in messages) + (len(json.dumps(schemas)) if schemas else 0)
            if pt > 200 and chars:
                conv = store.conversation(turn.conv_id)
                old = float(conv["settings"].get("cpt") or context.DEFAULT_CHARS_PER_TOKEN)
                new = max(1.5, min(6.0, chars / pt))
                store.patch_settings(turn.conv_id, {"cpt": round(old * 0.5 + new * 0.5, 3)})
            used = pt + (ot or 0)
            meta["context_used"] = used
            self._pub(turn.account["id"], "context", {"conv_id": turn.conv_id, "used": used, "num_ctx": num_ctx})

    # checks on the model's work ---------------------------------------------------------------------
    def _recovery(self, turn, blocks, model, kind, **fmt) -> str:
        """Tell the person what went wrong and return the note that sends the model back to work."""
        notice_text, nudge = RECOVERY[kind]
        notice = {"type": "notice", "text": notice_text}
        blocks.append(notice)
        self._block(turn, blocks, notice)
        self.app.stats.event("recovery", turn.account["id"], turn.conv_id, turn.window, kind=kind, model=model)
        return nudge.format(**fmt) if fmt else nudge

    def _unfinished(self, turn, store, blocks, prose, tool_list, checked) -> str | None:
        """Why the model should go back to work instead of ending its turn (a RECOVERY kind), or None: a tool
        call written as text, no reply at all, or a reply saying something was made or changed when nothing was."""
        tools = {t.name: t for t in tool_list}
        if tools and "text_call" not in checked and _looks_like_tool_call(prose, tools):
            return "text_call"
        if not prose.strip():
            return None if "empty_reply" in checked or _made_something(blocks, visible=True) else "empty_reply"
        if (tools and "unfinished_claim" not in checked and claims_done(prose) and not _made_something(blocks)
                and asks_for_work(self._request_text(store, turn))):
            return "unfinished_claim"
        return None

    @staticmethod
    def _request_text(store, turn) -> str:
        user = store.message(turn.user_msg_id) or {"blocks": []}
        return " ".join(b.get("expanded") or b.get("text", "") for b in user["blocks"] if b.get("type") == "text")

    def _promote_pages(self, turn, store, blocks, block, save: bool = True) -> None:
        """A complete web page or SVG image written in the reply becomes an artifact, shown as a card in place of
        its code. Long tool calls are where small models, and Ollama's reading of them, fail; a page written in the
        reply avoids both, and works with models that cannot call tools."""
        found = []
        for f in page_fences(block["text"]):
            a = self._page_artifact(turn, store, f, save)
            if a is not None:
                found.append({"start": f["start"], "end": f["end"], "id": a["id"], "version": a["version"],
                              "title": a["title"], "kind": a["kind"]})
        if found:
            block["artifacts"] = found
            self._block(turn, blocks, block)

    def _page_artifact(self, turn, store, f: dict, save: bool = True) -> dict | None:
        """The artifact for a page from the reply: a new version of this conversation's artifact with the same
        title (or page title), else a new artifact. With save=False only an existing match, unchanged (or None)."""
        title = f["title"] or page_title(f["content"]) or ("Web page" if f["kind"] == "html" else "SVG image")
        same = None
        mine = [a for a in store.artifacts(turn.conv_id) if a["kind"] == f["kind"]]
        want = page_title(f["content"])
        for a in reversed(mine):
            full = store.artifact(a["id"])
            if a["title"].strip().lower() == title.strip().lower() or (want and page_title(full["content"]) == want):
                same = full
                break
        if same is None and len(mine) == 1 and not want:
            same = store.artifact(mine[0]["id"])
        if same is not None and (same["content"] == f["content"] or not save):
            return same
        if not save:
            return None
        if same is not None:
            a, action = store.update_artifact(same["id"], f["content"], turn.assistant_id), "update"
        else:
            a, action = store.create_artifact(turn.conv_id, turn.assistant_id, clip(title, 120), f["kind"], f["content"]), "create"
        self._pub(turn.account["id"], "artifact", {"conv_id": turn.conv_id, "artifact": a})
        self.app.stats.event("artifact", turn.account["id"], turn.conv_id, turn.window, action=action, kind=a["kind"],
                             chars=len(f["content"]), source="reply")
        return a

    @staticmethod
    def _find_artifact(store, conv_id: str, key: str) -> dict | None:
        """This conversation's artifact by id, by title or by the end of its id (models shorten ids); when the
        conversation has a single artifact, that one."""
        key = str(key or "").strip()
        a = store.artifact(key) if key else None
        if a is not None and a["conv_id"] == conv_id:
            return a
        mine = store.artifacts(conv_id)
        k = key.lower().strip("`'\" ")
        for x in reversed(mine):
            if k and (x["title"].strip().lower() == k or (len(k) >= 6 and x["id"].lower().endswith(k))):
                return store.artifact(x["id"])
        return store.artifact(mine[0]["id"]) if len(mine) == 1 else None

    @staticmethod
    def _artifact_list(store, conv_id: str) -> str:
        mine = store.artifacts(conv_id)
        if not mine:
            return "This conversation has no artifacts yet."
        return "Artifacts in this conversation:\n" + "\n".join(
            f"- {a['id']}: {a['title']} ({a['kind']}, version {a['version']})" for a in mine)

    # live output -----------------------------------------------------------------------------------
    def _block(self, turn, blocks, block) -> None:
        self._flush(turn, blocks)
        self._pub(turn.account["id"], "msg.block", {"conv_id": turn.conv_id, "msg_id": turn.assistant_id,
                                                    "index": blocks.index(block), "block": block})

    def _delta(self, turn, blocks, block, text) -> None:
        key = turn.assistant_id
        pend = self._pending_delta.setdefault(key, {})
        idx = blocks.index(block)
        pend[idx] = pend.get(idx, "") + text
        now = mono_ms()
        if now - self._last_delta.get(key, 0) >= DELTA_INTERVAL_MS:
            self._flush(turn, blocks)

    def _flush(self, turn, blocks) -> None:
        key = turn.assistant_id
        pend = self._pending_delta.pop(key, None)
        self._last_delta[key] = mono_ms()
        if not pend:
            return
        for idx, text in pend.items():
            self._pub(turn.account["id"], "msg.delta", {"conv_id": turn.conv_id, "msg_id": key, "index": idx,
                                                        "kind": blocks[idx].get("type"), "text": text,
                                                        "len": len(blocks[idx].get("text", ""))})

    @property
    def _pending_delta(self) -> dict:
        return self._pd

    # tools -------------------------------------------------------------------------------------------
    async def _tool(self, turn, store, model, caps, blocks, call) -> None:
        fn = call.get("function") or {}
        name = fn.get("name") or ""
        args = fn.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {"_raw": args}
        block = {"type": "tool", "id": new_id(), "name": name, "args": args, "status": "pending"}
        blocks.append(block)
        self._block(turn, blocks, block)
        conv = store.conversation(turn.conv_id)
        if name.startswith("mcp__"):
            return await self._mcp_call(turn, store, conv, model, caps, blocks, block, name, args)
        tool = T.BY_NAME.get(name)
        workdir, wkind = self._workdir(turn, conv)
        allowed_names = {t.name for t in T.available(bool(workdir), conv["mode"], connectors=True, skills=True,
                                                     scratch=wkind == "scratch", **self._knowledge_tools(turn))}
        t0 = mono_ms()
        decision = None
        try:
            if tool is None or name not in allowed_names:
                raise T.ToolError(f"There is no tool called {name!r} here. Available: {', '.join(sorted(allowed_names))}")
            missing = [r for r in tool.required if r not in args]
            if missing:
                raise T.ToolError(f"Missing required argument(s): {', '.join(missing)}")
            for res in await self._hooks(turn, conv, "PreToolUse", {"tool": name, "args": args}, tool=name):
                if res["code"] == 2:
                    block["decision"] = {"action": "deny", "layer": "hook", "reason": res["stderr"].strip()}
                    block["status"] = "denied"
                    block["output"] = "Blocked by a hook: " + (res["stderr"].strip() or "no reason given")
                    return
            decision = await self._decide(turn, store, conv, model, caps, tool, args, block, blocks)
            block["decision"] = decision.public()
            if decision.action == "deny":
                block["status"] = "denied"
                block["output"] = f"Not allowed: {decision.reason}"
                return
            if decision.action == "ask":
                answer = await self._ask_approval(turn, store, conv, block, decision, blocks)
                if answer.get("decision") == "deny":
                    block["status"] = "denied"
                    note = answer.get("reason") or ""
                    block["output"] = "The user declined this action." + (f" Their note: {note}" if note else "")
                    return
                if answer.get("decision") == "allow_always" and answer.get("rule"):
                    store.add_rule("allow", answer["rule"])
                    self._pub(turn.account["id"], "rules", {})
            block["status"] = "running"
            self._block(turn, blocks, block)
            if tool.category in ("edit", "exec") and conv["folder"]:
                await self._checkpoint(turn, store, conv)
            before = None
            if wkind == "scratch" and tool.category in ("edit", "exec"):
                os.makedirs(os.path.join(workdir, "uploads"), exist_ok=True)
                before = await asyncio.to_thread(_snapshot, workdir)
            output, extra = await self._execute(turn, store, conv, model, caps, tool, args, block, blocks)
            if before is not None:
                made = _changed_files(before, await asyncio.to_thread(_snapshot, workdir))
                if made:
                    extra["files"] = made
                    self.app.stats.event("files_created", turn.account["id"], turn.conv_id, turn.window, tool=name,
                                         count=len(made), bytes=sum(f["size"] for f in made),
                                         kinds=sorted({os.path.splitext(f["path"])[1].lower() for f in made}))
            block["output"] = output
            block.update(extra)
            block["status"] = "error" if extra.get("failed") else "done"
            for res in await self._hooks(turn, conv, "PostToolUse", {"tool": name, "args": args, "output": clip(output, 20000)}, tool=name):
                if res["code"] == 2 and res["stderr"].strip():
                    block["output"] += "\n\n[A hook reports] " + clip(res["stderr"].strip(), 4000)
        except T.ToolError as exc:
            block["status"] = "error"
            block["output"] = f"Error: {exc}"
        except asyncio.CancelledError:
            block["status"] = "stopped"
            block["output"] = block.get("output") or "Stopped by the user."
            raise
        finally:
            block["duration_ms"] = round(mono_ms() - t0)
            self.app.stats.tool_call(
                account_id=turn.account["id"], conv_id=turn.conv_id, msg_id=turn.assistant_id, tool=name,
                mode=conv["mode"], decision=(block.get("decision") or {}).get("action"),
                layer=(block.get("decision") or {}).get("layer"), duration_ms=block["duration_ms"],
                exit_code=block.get("exit_code"), output_bytes=len(block.get("output") or ""),
                sandbox_denied=1 if block.get("sandbox_denied") else 0,
                error=block["output"][:200] if block["status"] == "error" else None)
            self._block(turn, blocks, block)

    async def _mcp_tools(self, account_id: str) -> list[dict]:
        if not any(c.get("enabled", True) for c in self.app.mcp.configs(account_id)):
            return []
        try:
            return await self.app.mcp.all_tools(account_id)
        except Exception:  # a broken connector must not break the conversation
            return []

    async def _mcp_call(self, turn, store, conv, model, caps, blocks, block, name, args) -> None:
        from ..mcp import MCPError
        t0 = mono_ms()
        tools = {t["id"]: t for t in await self._mcp_tools(turn.account["id"])}
        mt = tools.get(name)
        try:
            if mt is None:
                raise T.ToolError(f"No connector tool {name}. Use find_tools to look for one.")
            block["server"] = mt["server"]
            category = "mcp_ro" if mt["read_only"] else "mcp"
            ctx = self._context(turn, conv)
            decision = permissions.decide(name, category, args, ctx)
            if decision.action == "judge":
                decision = await self._judge_generic(turn, store, conv, model, caps, block, blocks, name, args, decision)
            block["decision"] = decision.public()
            if decision.action == "deny":
                block["status"], block["output"] = "denied", f"Not allowed: {decision.reason}"
                return
            if decision.action == "ask":
                answer = await self._ask_approval(turn, store, conv, block, decision, blocks)
                if answer.get("decision") == "deny":
                    block["status"] = "denied"
                    block["output"] = "The user declined this action." + (f" Their note: {answer['reason']}" if answer.get("reason") else "")
                    return
                if answer.get("decision") == "allow_always" and answer.get("rule"):
                    store.add_rule("allow", answer["rule"])
            block["status"] = "running"
            self._block(turn, blocks, block)
            res = await self.app.mcp.call(turn.account["id"], name, args)
            out, truncated = T.cap_output(res["text"])
            block["output"], block["status"] = out, ("error" if res["error"] else "done")
        except (T.ToolError, MCPError) as exc:
            block["status"], block["output"] = "error", f"Error: {exc}"
        except asyncio.CancelledError:
            block["status"], block["output"] = "stopped", "Stopped by the user."
            raise
        finally:
            block["duration_ms"] = round(mono_ms() - t0)
            self.app.stats.tool_call(account_id=turn.account["id"], conv_id=turn.conv_id, msg_id=turn.assistant_id,
                                     tool=name, mode=conv["mode"], decision=(block.get("decision") or {}).get("action"),
                                     layer=(block.get("decision") or {}).get("layer"), duration_ms=block["duration_ms"],
                                     output_bytes=len(block.get("output") or ""),
                                     error=block["output"][:200] if block["status"] == "error" else None)
            self._block(turn, blocks, block)

    async def _judge_generic(self, turn, store, conv, model, caps, block, blocks, name, args, decision) -> Decision:
        judge_model = self._judge_model(model)
        if judge_model is None:
            return Decision("ask", "judge", "No approved model is large enough to judge actions safely, so you decide.")
        jcaps = (self.app.registry.get(judge_model) or {}).get("info", {}).get("capabilities") or caps
        requests = [clip(" ".join(b.get("text", "") for b in m["blocks"] if b.get("type") == "text"), 600)
                    for m in store.path(turn.user_msg_id) if m["role"] == "user"][-3:]
        block["status"] = "checking"
        self._block(turn, blocks, block)
        verdict = await judge.judge(self.app.gateway, judge_model, jcaps, name, args, conv["folder"], decision.reason,
                                    requests, store.auto_rules(), turn.account["id"], turn.conv_id, turn.window)
        block["judge"] = verdict
        return Decision("allow" if verdict["allowed"] else "ask", "judge", verdict["reason"], extra={"judge": verdict})

    def _workdir(self, turn, conv) -> tuple[str | None, str]:
        """Where file tools and commands work: the conversation's folder ('folder'), else the chat's own scratch
        folder ('scratch') when code execution is on, else nowhere ('none')."""
        if conv["folder"]:
            return conv["folder"], "folder"
        if (turn.account.get("settings") or {}).get("code_exec", True) is False:
            return None, "none"
        return str(self.app.paths.account(turn.account["id"]) / "files" / "scratch" / conv["id"]), "scratch"

    def _context(self, turn, conv) -> permissions.Context:
        account = turn.account
        folder = conv["folder"]
        scratch = None
        if not folder:
            workdir, wkind = self._workdir(turn, conv)
            if wkind == "scratch":
                scratch = workdir
                os.makedirs(os.path.join(workdir, "uploads"), exist_ok=True)
        grants = self.app.accounts.grants(account["id"])
        readable = [folder] if folder else [scratch] if scratch else []
        writable = [folder] if folder else [scratch] if scratch else []
        if folder and account["role"] != "owner":
            access = self.app.accounts.folder_access(account, folder)
            if access != "rw":
                writable = []
            if access is None:
                readable = []
        readable += [g["path"] for g in grants]
        readable.append(str(self.app.paths.account_files(account["id"], "extend")))
        writable += [g["path"] for g in grants if g["access"] == "rw"]
        store = self.app.stores.get(account["id"])
        # Modes are for folder work, where the mode is shown. A chat without a folder never plans: its connector
        # actions ask first, as in manual mode.
        mode = conv["mode"] if folder or conv["mode"] != "plan" else "manual"
        return permissions.Context(mode=mode, folder=folder or scratch, rules=store.rules(), readable=readable,
                                   writable=writable, network=bool(conv["settings"].get("network")),
                                   is_owner=account["role"] == "owner", scratch=scratch)

    async def _decide(self, turn, store, conv, model, caps, tool, args, block, blocks=None) -> Decision:
        ctx = self._context(turn, conv)
        decision = permissions.decide(tool.name, tool.category, args, ctx)
        if decision.action != "judge":
            return decision
        judge_model = self._judge_model(model)
        if judge_model is None:
            return Decision("ask", "judge", "No approved model is large enough to judge actions safely, so you decide.")
        jcaps = (self.app.registry.get(judge_model) or {}).get("info", {}).get("capabilities") or caps
        requests = [clip(" ".join(b.get("text", "") for b in m["blocks"] if b.get("type") == "text"), 600)
                    for m in store.path(turn.user_msg_id) if m["role"] == "user"][-3:]
        block["status"] = "checking"
        if blocks is not None:
            self._block(turn, blocks, block)
        verdict = await judge.judge(self.app.gateway, judge_model, jcaps, tool.name, args, conv["folder"], decision.reason,
                                    requests, store.auto_rules(), turn.account["id"], turn.conv_id, turn.window)
        block["judge"] = verdict
        if verdict["allowed"]:
            return Decision("allow", "judge", verdict["reason"], extra={"judge": verdict})
        return Decision("ask", "judge", verdict["reason"], extra={"judge": verdict})

    def _judge_model(self, model: str) -> str | None:
        """The model that judges auto-mode actions: the owner's choice, else the conversation's model, else
        the default. Models under 3B parameters let risky actions through in measurements, so never those."""
        reg = self.app.registry
        for name in (self.app.maindb.get_setting("judge_model"), model, reg.default_chat()):
            m = reg.get(name) if name else None
            if m and m["approved"] and (m["param_b"] or 0) >= JUDGE_MIN_PARAMS_B:
                return name
        return None

    async def _ask_approval(self, turn, store, conv, block, decision, blocks=None) -> dict:
        account = turn.account
        owners = [a for a in self.app.accounts.owners()]
        if account.get("require_owner_approval") and account["role"] != "owner":
            answerers = {a["id"] for a in owners}
        else:
            answerers = {account["id"]}
        payload = {"id": new_id(), "kind": "approval", "conv_id": turn.conv_id, "conv_title": conv["title"],
                   "account_id": account["id"], "account_name": account["display_name"], "block_id": block["id"],
                   "msg_id": turn.assistant_id, "tool": block["name"], "args": block["args"],
                   "decision": decision.public(), "suggested_rule": suggest_rule(block["name"], block["args"], conv["folder"]),
                   "created_ms": now_ms(), "for_owner": answerers != {account["id"]}}
        block["status"] = "waiting"
        block["approval_id"] = payload["id"]
        block["decision"] = decision.public()
        if blocks is not None:
            self._block(turn, blocks, block)
        answer = await self._wait_for_person(turn, payload, answerers)
        verdict = (decision.extra or {}).get("judge") or {}
        self.app.stats.approval(
            account_id=account["id"], conv_id=turn.conv_id, tool_call_id=block["id"], tool=block["name"],
            mode=conv["mode"], layer=decision.layer, judge_risk=verdict.get("risk"),
            judge_user_asked=verdict.get("user_asked"), judge_decision=verdict.get("decision"),
            final=answer.get("decision"), approver_id=answer.get("by"), latency_ms=answer.get("latency_ms"))
        return answer

    async def _wait_for_person(self, turn, payload, answerers: set) -> dict:
        fut = asyncio.get_running_loop().create_future()
        self.pending[payload["id"]] = {"future": fut, "payload": payload, "answerers": answerers, "conv_id": turn.conv_id}
        # the conversation's windows show the request; the answerers get it wherever they are
        for aid in answerers | {turn.account["id"]}:
            self._pub(aid, "pending", payload)
        turn.state = "waiting"
        self._turn_state(turn)
        t0 = mono_ms()
        try:
            answer = await fut
            answer["latency_ms"] = round(mono_ms() - t0)
            return answer
        finally:
            self.pending.pop(payload["id"], None)
            for aid in answerers | {turn.account["id"]}:
                self._pub(aid, "pending.done", {"id": payload["id"], "conv_id": turn.conv_id})
            turn.state = "running"
            self._turn_state(turn)

    async def _checkpoint(self, turn, store, conv) -> None:
        if turn.checkpointed:
            return
        turn.checkpointed = True
        if store.checkpoint_for(turn.user_msg_id):
            return
        folder = conv["folder"]
        prev = store.latest_checkpoint(turn.conv_id, folder)
        try:
            manifest = await asyncio.to_thread(self.app.checkpoints.snapshot, turn.account["id"], folder,
                                               prev["manifest"] if prev else None)
        except (checkpoints.TooLarge, OSError) as exc:
            self._pub(turn.account["id"], "notice", {"conv_id": turn.conv_id, "text": f"No file checkpoint: {exc}"})
            return
        store.add_checkpoint(turn.conv_id, turn.user_msg_id, folder, manifest)

    async def _execute(self, turn, store, conv, model, caps, tool, args, block, blocks) -> tuple[str, dict]:
        name, folder = tool.name, self._workdir(turn, conv)[0]
        ctx = self._context(turn, conv)
        if name == "create_file":
            from .. import writers
            path = T.resolve(args.get("path") or "", folder)
            if not any(permissions.inside(path, w) for w in ctx.writable):
                raise T.ToolError("Files can only be created inside the working folder")
            content = T.unescape_content(str(args.get("content") or ""), os.path.splitext(path)[1])
            try:
                r = await asyncio.to_thread(writers.create, path, content, args.get("title") or None)
            except (writers.WriterError, OSError) as exc:
                raise T.ToolError(str(exc)) from exc
            rel = os.path.relpath(path, folder)
            self.app.stats.event("file_created", turn.account["id"], turn.conv_id, turn.window, kind=r["kind"], bytes=r["bytes"])
            return f"Created {rel} ({r['bytes']:,} bytes). The user can download it from the conversation.", {"path": path}
        if name in ("read_file", "edit_file") and not conv["folder"] and store.artifacts(turn.conv_id) \
                and not os.path.exists(T.resolve(args.get("path") or "", folder)):
            # small models take the conversation's artifacts for files in the chat's folder
            raise T.ToolError(f"There is no file {args.get('path')!r} in this chat's folder. Artifacts are not files: read one "
                              "with read_artifact, change it with update_artifact or rewrite_artifact. "
                              + self._artifact_list(store, turn.conv_id))
        if name == "read_file":
            return await asyncio.to_thread(T.read_file, args, folder), {}
        if name == "list_files":
            return await asyncio.to_thread(T.list_files, args, folder), {}
        if name == "search":
            return await asyncio.to_thread(T.search, args, folder), {}
        if name == "write_file":
            r = await asyncio.to_thread(T.write_file, args, folder)
            return r["output"], {"diff": r["diff"], "path": r["path"]}
        if name == "edit_file":
            r = await asyncio.to_thread(T.edit_file, args, folder)
            return r["output"], {"diff": r["diff"], "path": r["path"]}
        if name == "bash":
            return await self._bash(turn, conv, ctx, args, block, blocks)
        if name == "bash_output":
            try:
                r = self.app.shells.read_background(args["id"], turn.conv_id)
            except KeyError as exc:
                raise T.ToolError(f"No background command {args['id']}") from exc
            state = "still running" if r["running"] else f"finished with exit code {r['exit_code']}"
            return f"[{r['id']} {state}]\n{r['output'] or '(no new output)'}", {}
        if name == "kill_shell":
            try:
                killed = self.app.shells.kill_background(args["id"], turn.conv_id)
            except KeyError as exc:
                raise T.ToolError(f"No background command {args['id']}") from exc
            return ("Stopped." if killed else "It had already finished."), {}
        if name == "web_fetch":
            return await T.web_fetch(args), {}
        if name == "web_search":
            return await T.web_search(args), {}
        if name == "todo_write":
            todos = [t for t in (args.get("todos") or []) if isinstance(t, dict)]
            store.patch_settings(turn.conv_id, {"todos": todos})
            self._pub(turn.account["id"], "todos", {"conv_id": turn.conv_id, "todos": todos})
            return "Task list updated:\n" + T.todos_text(todos), {}
        if name == "create_artifact":
            kind = args.get("kind") if args.get("kind") in ("html", "svg", "markdown", "slides", "code", "mermaid") else "markdown"
            content = T.unescape_content(str(args.get("content") or ""), kind)
            a = store.create_artifact(turn.conv_id, turn.assistant_id, clip(args.get("title") or "Untitled", 120), kind,
                                      content, args.get("language"))
            self._pub(turn.account["id"], "artifact", {"conv_id": turn.conv_id, "artifact": a})
            self.app.stats.event("artifact", turn.account["id"], turn.conv_id, turn.window, action="create", kind=kind,
                                 chars=len(a["content"]))
            return f"Created artifact {a['id']} ({kind}): {a['title']}", {"artifact_id": a["id"], "artifact_version": 1}
        if name in ("update_artifact", "rewrite_artifact"):
            a = self._find_artifact(store, turn.conv_id, args.get("id"))
            if a is None:
                raise T.ToolError(f"No artifact {args.get('id')!r} in this conversation. {self._artifact_list(store, turn.conv_id)}")
            if name == "update_artifact":
                content, _how, _n = T.apply_edit(a["content"], args.get("old_text", ""), args.get("new_text", ""))
            else:
                content = T.unescape_content(str(args.get("content") or ""), a["kind"])
            a = store.update_artifact(a["id"], content, turn.assistant_id, args.get("title"))
            self._pub(turn.account["id"], "artifact", {"conv_id": turn.conv_id, "artifact": a})
            self.app.stats.event("artifact", turn.account["id"], turn.conv_id, turn.window, action="update", kind=a["kind"])
            return f"Updated artifact {a['id']} to version {a['version']}", {"artifact_id": a["id"], "artifact_version": a["version"]}
        if name == "read_artifact":
            if not str(args.get("id") or "").strip():
                return self._artifact_list(store, turn.conv_id), {}
            a = self._find_artifact(store, turn.conv_id, args.get("id"))
            if a is None:
                raise T.ToolError(f"No artifact {args.get('id')!r} in this conversation. {self._artifact_list(store, turn.conv_id)}")
            body = a["content"] if len(a["content"]) <= 60000 else a["content"][:60000] + "\n[… cut at 60,000 characters]"
            return f"Artifact {a['id']} ({a['kind']}), version {a['version']}: {a['title']}\n\n{body}", {}
        if name == "ask_user":
            payload = {"id": new_id(), "kind": "question", "conv_id": turn.conv_id, "account_id": turn.account["id"],
                       "block_id": block["id"], "msg_id": turn.assistant_id, "question": args.get("question", ""),
                       "options": [str(o) for o in (args.get("options") or [])][:8], "created_ms": now_ms()}
            block["status"] = "waiting"
            self._block(turn, blocks, block)
            answer = await self._wait_for_person(turn, payload, {turn.account["id"]})
            return f"The user answered: {answer.get('text', '')}", {"answer": answer.get("text", "")}
        if name == "propose_plan":
            payload = {"id": new_id(), "kind": "plan", "conv_id": turn.conv_id, "account_id": turn.account["id"],
                       "block_id": block["id"], "msg_id": turn.assistant_id, "plan": args.get("plan", ""),
                       "created_ms": now_ms()}
            block["status"] = "waiting"
            self._block(turn, blocks, block)
            answer = await self._wait_for_person(turn, payload, {turn.account["id"]})
            if answer.get("decision") == "approve":
                mode = answer.get("mode") if answer.get("mode") in permissions.MODES and answer.get("mode") != "plan" else "accept_edits"
                store.update_conversation(turn.conv_id, mode=mode)
                self._pub(turn.account["id"], "conv", {"conversation": store.conversation(turn.conv_id)})
                return (f"The user approved the plan. The mode is now {permissions.MODE_LABELS[mode]}. "
                        "Carry out the plan now, step by step."), {"approved": True, "mode": mode}
            fb = answer.get("text") or "no details given"
            return f"The user wants changes to the plan: {fb}. Revise it and propose it again.", {"approved": False}
        if name == "task":
            return await self._subagent(turn, store, conv, model, caps, args, block, blocks)
        if name == "use_skill":
            real = conv["folder"]
            trusted = bool(real) and self.app.maindb.is_trusted(real)
            skill = self.app.extend.skills(turn.account["id"], real, trusted).get((args.get("name") or "").strip())
            if skill is None:
                raise T.ToolError("No skill by that name. The Skills list in your instructions has the names.")
            self.app.stats.event("skill_used", turn.account["id"], turn.conv_id, turn.window, skill=skill["name"], source=skill["source"])
            return self.app.extend.load_skill(skill), {"skill": skill["name"]}
        if name == "generate_image":
            b = await self._make_image(turn, store, blocks, str(args.get("prompt") or ""), args.get("shape") or "square",
                                       bool(args.get("edit")))
            return (f"Made the image ({b['width']}×{b['height']}, seed {b['seed']}) with {b['model']}; the user can see it "
                    "in the conversation. Describe it briefly only if useful."), {"image": b["id"]}
        if name == "search_project":
            if not turn.project:
                raise T.ToolError("This conversation is not in a project")
            from ..knowledge import Knowledge
            res = await self.app.knowledge.search(turn.account["id"], turn.project["id"], str(args.get("query") or ""),
                                                  k=max(1, min(12, int(args.get("limit") or 6))), conv_id=turn.conv_id,
                                                  window=turn.window)
            return Knowledge.format_results(res), {"sources": _sources(res)}
        if name == "memory":
            return self._memory_tool(turn, store, args), {}
        if name == "past_chats":
            return self._past_chats(turn, store, args), {}
        if name == "find_tools":
            found = await self.app.mcp.search(turn.account["id"], args.get("query") or "")
            if not found:
                return "No connector tools match. Connectors are added in Settings > Connectors.", {}
            active = [x for x in (conv["settings"].get("active_tools") or []) if x not in {f["id"] for f in found}]
            active = (active + [f["id"] for f in found])[-12:]
            store.patch_settings(turn.conv_id, {"active_tools": active})
            lines = [f"- {f['id']}: {f['description'][:200]}" + (" (read-only)" if f["read_only"] else "") for f in found]
            return "These tools are now available; call them by name:\n" + "\n".join(lines), {}
        raise T.ToolError(f"{name} is not implemented")

    # knowledge: projects, memory, earlier conversations -------------------------------------------
    def _knowledge_tools(self, turn) -> dict:
        return {"project_search": turn.project_mode == "search", "memory": turn.memory_on, "past_chats": turn.past_chats_on,
                "images": bool(self.app.registry.image_model())}

    # images -----------------------------------------------------------------------------------------
    SHAPES = {"square": (1024, 1024), "portrait": (832, 1216), "landscape": (1216, 832), "wide": (1344, 768)}

    def _last_image(self, store, turn) -> dict | None:
        """The image to edit: one attached to the user's message, else the latest image in the conversation."""
        user = store.message(turn.user_msg_id)
        for b in user["blocks"]:
            if b.get("type") == "attachment" and b.get("kind") == "image":
                return store.attachment(b["id"])
        for m in reversed(store.path(turn.user_msg_id)):
            for b in reversed(m["blocks"]):
                if b.get("type") == "image" or (b.get("type") == "attachment" and b.get("kind") == "image"):
                    return store.attachment(b["id"])
        return None

    async def _make_image(self, turn, store, blocks, prompt: str, shape: str = "square", edit: bool = False,
                          seed: int | None = None) -> dict:
        """Make (or edit) an image with the approved image model; add it to the reply as an image block."""
        from .. import sdcpp
        model = self.app.registry.image_model(turn.account.get("settings", {}).get("image_model"))
        if not model:
            raise T.ToolError("No image model is approved on this computer yet")
        info = (self.app.registry.get(model) or {}).get("info", {})
        ref = None
        if edit:
            if "edit" not in (info.get("capabilities") or []):  # the chosen model only draws: use one that edits
                editors = [m for m in self.app.registry.approved("image") if "edit" in (m["info"].get("capabilities") or [])]
                if not editors:
                    raise T.ToolError(f"No image model here can edit images ({model} only draws new ones); describe a new image instead")
                model, info = editors[0]["name"], editors[0]["info"]
            ref = self._last_image(store, turn)
            if ref is None:
                raise T.ToolError("There is no image to change; attach one or make one first")
        w, h = self.SHAPES.get(shape or "square", self.SHAPES["square"])
        seed = sdcpp.new_seed() if seed is None else seed
        block = {"type": "image", "id": None, "prompt": clip(prompt, 2000), "model": model, "status": "running",
                 "width": w, "height": h, "seed": seed, "edit": bool(ref)}
        blocks.append(block)
        self._block(turn, blocks, block)

        def progress(p):
            block["progress"] = p
            self._pub(turn.account["id"], "image.progress", {"conv_id": turn.conv_id, "msg_id": turn.assistant_id,
                                                             "index": blocks.index(block), **p})
        req = {"prompt": prompt, "width": w, "height": h, "seed": seed}
        if ref:
            with open(ref["path"], "rb") as f:
                req["ref_images"] = [f.read()]
        try:
            res = await self.app.gateway.image(model, req, account_id=turn.account["id"], conv_id=turn.conv_id,
                                               msg_id=turn.assistant_id, window=turn.window, on_progress=progress)
        except BaseException as exc:
            block["status"] = "stopped" if isinstance(exc, asyncio.CancelledError) else "error"
            block["error"] = "" if block["status"] == "stopped" else str(exc)[:400]
            self._block(turn, blocks, block)
            raise
        png = res["images"][0]
        aid = new_id()
        folder = self.app.paths.account_files(turn.account["id"], "uploads", aid)
        name = "image-" + time_stamp() + ".png"
        path = folder / name
        with open(path, "wb") as f:
            f.write(png)
        att = store.add_attachment(name, "image/png", len(png), "image", path, None,
                                   {"generated": True, "prompt": clip(prompt, 2000), "model": model, "seed": seed, "width": w,
                                    "height": h, "seconds": res["seconds"], "edit_of": ref["id"] if ref else None},
                                   conv_id=turn.conv_id)
        block.update({"id": att["id"], "status": "done", "seconds": res["seconds"]})
        block.pop("progress", None)
        self._block(turn, blocks, block)
        self.app.stats.event("image_generated", turn.account["id"], turn.conv_id, turn.window, model=model, width=w, height=h,
                             edit=bool(ref), seconds=res["seconds"], bytes=len(png))
        return block

    async def _prepare_knowledge(self, turn, store, model, blocks) -> None:
        """Once per turn: the project's part of the prompt, excerpts for this message, and saved memories."""
        from ..knowledge import Knowledge
        conv = store.conversation(turn.conv_id)
        settings = turn.account.get("settings") or {}
        incognito = bool(conv["incognito"])
        turn.memory_on = not incognito and settings.get("memory", True) is not False
        turn.past_chats_on = not incognito and settings.get("chat_search", True) is not False
        turn.project = store.project(conv["project_id"]) if conv.get("project_id") else None
        if turn.project:
            num_ctx = self.app.registry.num_ctx(model)
            turn.project_prompt, turn.project_mode = self.app.knowledge.prompt(turn.account["id"], turn.project, num_ctx)
            query = self._retrieval_query(store, turn.user_msg_id) if turn.project_mode == "search" else ""
            if query:
                res = await self.app.knowledge.search(turn.account["id"], turn.project["id"], query, conv_id=turn.conv_id,
                                                      window=turn.window)
                text = Knowledge.format_results(res)
                caps = (self.app.registry.get(model) or {}).get("info", {}).get("capabilities") or []
                if "tools" in caps:
                    # as if the model had searched: stays in the history, and the prompt's start is unchanged
                    block = {"type": "tool", "id": new_id(), "name": "search_project", "args": {"query": clip(query, 300)},
                             "status": "done", "output": text, "auto": True, "sources": _sources(res)}
                else:
                    turn.project_prompt += "\n\n## Excerpts for this message\n" + text
                    block = {"type": "retrieval", "query": clip(query, 300), "sources": _sources(res)}
                blocks.append(block)
                self._block(turn, blocks, block)
        if turn.memory_on:
            turn.memory_prompt = prompts.memory_section(store.memories(turn.project["id"] if turn.project else None))

    async def _prepare_scratch(self, turn, store) -> None:
        """A chat's attachments are copied into its scratch folder (uploads/) for code to work on."""
        conv = store.conversation(turn.conv_id)
        workdir, wkind = self._workdir(turn, conv)
        if wkind != "scratch":
            return
        user = store.message(turn.user_msg_id)
        atts = [store.attachment(b.get("id")) for b in user["blocks"] if b.get("type") == "attachment"]
        atts = [a for a in atts if a and a.get("path")]
        if not atts:
            return
        import shutil
        up = os.path.join(workdir, "uploads")
        os.makedirs(up, exist_ok=True)
        for a in atts:
            dest = os.path.join(up, os.path.basename(a["name"]))
            if not os.path.exists(dest) and os.path.getsize(a["path"]) <= 500 * 2**20:
                await asyncio.to_thread(shutil.copyfile, a["path"], dest)  # a copy: code may change it

    @staticmethod
    def _retrieval_query(store, user_msg_id) -> str:
        """The user's message, with the previous one in front when it is a short follow-up."""
        path = [m for m in store.path(user_msg_id) if m["role"] == "user"]
        text = lambda m: " ".join(b.get("expanded") or b.get("text", "") for b in m["blocks"] if b.get("type") == "text")  # noqa: E731
        q = text(path[-1]).strip() if path else ""
        if len(q.split()) < 8 and len(path) > 1:
            q = (clip(text(path[-2]).strip(), 300) + " " + q).strip()
        return clip(q, 1000)

    def _memory_tool(self, turn, store, args) -> str:
        action = (args.get("action") or "").strip().lower()
        text = " ".join(str(args.get("text") or "").split())
        project_id = turn.project["id"] if turn.project else None
        if action == "add":
            if not text:
                raise T.ToolError("Give the fact to remember in text")
            if len(text) > 500:
                raise T.ToolError("Keep a memory to one or two sentences (under 500 characters)")
            existing = store.memories(project_id)
            if any(m["text"].strip().lower() == text.lower() for m in existing):
                return "That is already in memory."
            if len(existing) >= 300:
                raise T.ToolError("Memory is full (300 items). Ask the user to remove some in Settings > Memory.")
            m = store.add_memory(text, project_id, "model", turn.conv_id)
            where = f" (for the project {turn.project['name']})" if project_id else ""
            result = f"Saved to memory{where} [{m['id'][-6:]}]: {text}"
        elif action in ("update", "delete"):
            short = str(args.get("id") or "").strip().strip("[]").lower()
            hits = [m for m in store.memories(project_id) if short and m["id"].lower().endswith(short)]
            if len(hits) != 1:
                raise T.ToolError("No memory with that id; the ids are in brackets in the Memory list")
            m = hits[0]
            if action == "update":
                if not text:
                    raise T.ToolError("Give the new text")
                store.update_memory(m["id"], text)
                result = f"Memory [{m['id'][-6:]}] updated: {text}"
            else:
                store.delete_memory(m["id"])
                result = f"Memory [{m['id'][-6:]}] deleted."
        else:
            raise T.ToolError("action must be add, update or delete")
        self.app.stats.event("memory_" + action, turn.account["id"], turn.conv_id, turn.window, by="model",
                             project=bool(project_id))
        self._pub(turn.account["id"], "memory", {})
        return result

    def _past_chats(self, turn, store, args) -> str:
        import time as _time
        limit = max(1, min(10, int(args.get("limit") or 5)))
        query = str(args.get("query") or "").strip()
        project_id = turn.project["id"] if turn.project else None
        day = lambda ms: _time.strftime("%Y-%m-%d", _time.localtime(ms / 1000))  # noqa: E731
        if query:
            hits = store.search(query, limit=limit, any_term=True, project_id=project_id, exclude=turn.conv_id)
            if not hits:
                return f"No earlier conversation matches {query!r}."
            seen, lines = set(), []
            for h in hits:
                if h["conv_id"] in seen:
                    continue
                seen.add(h["conv_id"])
                snippet = h["snippet"].replace("[[", "").replace("]]", "")
                lines.append(f"- \"{h['title'] or 'Untitled'}\" ({day(h['updated_ms'])}): … {snippet} …")
            self.app.stats.event("past_chats", turn.account["id"], turn.conv_id, turn.window, mode="search", results=len(lines))
            return f"Earlier conversations matching {query!r}:\n" + "\n".join(lines)
        convs = [c for c in store.conversations(limit=limit + 1, project_id=project_id) if c["id"] != turn.conv_id][:limit]
        lines = []
        for c in convs:
            first = next((m for m in store.path(c["leaf_id"]) if m["role"] == "user"), None) if c["leaf_id"] else None
            opening = clip(" ".join(b.get("text", "") for b in first["blocks"] if b.get("type") == "text"), 200) if first else ""
            lines.append(f"- \"{c['title'] or 'Untitled'}\" ({day(c['updated_ms'])}): {opening}")
        self.app.stats.event("past_chats", turn.account["id"], turn.conv_id, turn.window, mode="recent", results=len(lines))
        return ("The most recent earlier conversations:\n" + "\n".join(lines)) if lines else "There are no earlier conversations."

    async def _bash(self, turn, conv, ctx, args, block, blocks) -> tuple[str, dict]:
        command = args.get("command") or ""
        folder = self._workdir(turn, conv)[0]
        network = bool(args.get("network")) or ctx.network
        if args.get("background") and self._workdir(turn, conv)[1] != "scratch":  # a chat has no bash_output: run it now
            sid = await self.app.shells.start_background(turn.conv_id, turn.account["id"], command, folder,
                                                         ctx.writable, ctx.readable, network)
            return f"Started in the background with id {sid}. Read its output with bash_output.", {"shell_id": sid}
        timeout = max(1, min(1800, int(args.get("timeout") or 120)))
        live = {"buf": "", "t": 0.0}

        def on_output(text):
            live["buf"] += text
            now = mono_ms()
            if now - live["t"] > 150:
                self._pub(turn.account["id"], "tool.output", {"conv_id": turn.conv_id, "msg_id": turn.assistant_id,
                                                              "block_id": block["id"], "text": live["buf"]})
                live["buf"], live["t"] = "", now

        r = await self.app.shells.run(turn.account["id"], command, folder, ctx.writable, ctx.readable, network,
                                      timeout, on_output)
        if live["buf"]:
            on_output("")
        out = r["output"].rstrip("\n")
        tail = f"\n[exit code {r['exit_code']}]" if r["exit_code"] else ""
        extra = {"exit_code": r["exit_code"], "truncated": r["truncated"], "sandbox_denied": r["denied"],
                 "failed": r["denied"]}
        return (out + tail) if out else f"(no output){tail}", extra

    async def _subagent(self, turn, store, conv, model, caps, args, block, blocks) -> tuple[str, dict]:
        folder = conv["folder"]
        trusted = bool(folder) and self.app.maindb.is_trusted(folder)
        custom = self.app.extend.agents(turn.account["id"], folder, trusted).get(args.get("agent") or "")
        kind = args.get("agent") if args.get("agent") in ("explore", "general") else ("general" if custom else "explore")
        tool_list = T.available(bool(folder), conv["mode"], subagent=kind)
        if custom and custom["meta"].get("tools"):
            allowed = set(custom["meta"]["tools"] if isinstance(custom["meta"]["tools"], list) else
                          [x.strip() for x in str(custom["meta"]["tools"]).split(",")])
            tool_list = [x for x in tool_list if x.name in allowed]
        schemas = [t.schema() for t in tool_list]
        role = (custom["body"].strip() + "\n\n") if custom else ""
        system = role + prompts.SUBAGENT + "\n\n" + prompts.system_prompt(model, folder, conv["mode"],
                                                                           bool(conv["settings"].get("network")), tools=True)
        if custom and custom["meta"].get("model") in [m["name"] for m in self.app.registry.approved("chat")]:
            model = custom["meta"]["model"]
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": args.get("prompt", "")}]
        num_ctx = self.app.registry.num_ctx(model)
        steps = []
        block["steps"] = steps
        for _ in range(30):
            msgs, _ = context.clear_old_tool_output(msgs, int(num_ctx * 0.6), context.DEFAULT_CHARS_PER_TOKEN)
            res = await self.app.gateway.complete(
                model=model, messages=msgs, tools=schemas, think=False if "thinking" in caps else None, kind="subagent",
                account_id=turn.account["id"], conv_id=turn.conv_id, msg_id=turn.assistant_id, window=turn.window,
                label=clip(args.get("description", "helper"), 40))
            msgs.append({"role": "assistant", "content": res["content"], "tool_calls": res["tool_calls"] or None})
            if not res["tool_calls"]:
                return res["content"] or "(the helper returned nothing)", {"steps": steps}
            for call in res["tool_calls"]:
                sub = {"type": "tool", "id": new_id(), "name": (call.get("function") or {}).get("name", ""),
                       "args": (call.get("function") or {}).get("arguments") or {}, "status": "running"}
                steps.append({"name": sub["name"], "args": _short_args(sub["args"]), "status": "running"})
                self._block(turn, blocks, block)
                tool = T.BY_NAME.get(sub["name"])
                try:
                    if tool is None or tool not in tool_list:
                        raise T.ToolError(f"The helper cannot use {sub['name']}")
                    decision = await self._decide(turn, store, conv, model, caps, tool, sub["args"], sub)
                    if decision.action == "deny":
                        raise T.ToolError(f"Not allowed: {decision.reason}")
                    if decision.action == "ask":
                        answer = await self._ask_approval(turn, store, conv, sub, decision)
                        if answer.get("decision") == "deny":
                            raise T.ToolError("The user declined this action")
                    out, _extra = await self._execute(turn, store, conv, model, caps, tool, sub["args"], sub, blocks)
                    steps[-1]["status"] = "done"
                except T.ToolError as exc:
                    out = f"Error: {exc}"
                    steps[-1]["status"] = "error"
                msgs.append({"role": "tool", "tool_name": sub["name"], "content": out})
                self._block(turn, blocks, block)
        return "The helper stopped after 30 steps without a final report.", {"steps": steps}

    # compaction, titles, rewind -------------------------------------------------------------------
    async def compact(self, account: dict, conv_id: str, before: str | None = None, model: str | None = None,
                      window: str = "browser", reason: str = "manual") -> dict:
        """Summarize the conversation. `before`: summarize everything before this message (it is kept)."""
        store = self.app.stores.get(account["id"])
        conv = store.conversation(conv_id)
        model = model or conv["model"] or self.app.registry.default_chat()
        if not model:
            raise ModelNotAllowed("No approved model")
        if before:
            anchor = store.message(before)
            upto = anchor["parent_id"]
        else:
            upto = conv["leaf_id"]
        if not upto:
            raise ValueError("Nothing to compact yet")
        thread = store.path(upto)
        num_ctx = self.app.registry.num_ctx(model)
        caps = (self.app.registry.get(model) or {}).get("info", {}).get("capabilities") or []
        budget_chars = int(num_ctx * 0.6 * context.DEFAULT_CHARS_PER_TOKEN)
        text = context.transcript(thread, budget_chars)
        messages = [{"role": "system", "content": "You summarize conversations between a user and an AI assistant, accurately and densely."},
                    {"role": "user", "content": prompts.COMPACT + "\n\n<conversation>\n" + text + "\n</conversation>"}]
        tokens_before = context.estimate_tokens(context.build(thread, "", store, False))
        self._pub(account["id"], "notice", {"conv_id": conv_id, "text": "Compacting the conversation…"})
        res = await self.app.gateway.complete(
            model=model, kind="compact", messages=messages, think=False if "thinking" in caps else None,
            options={"temperature": 0.2}, account_id=account["id"], conv_id=conv_id, window=window,
            label="compacting")
        summary = res["content"].strip() or "(empty summary)"
        node = store.add_message(conv_id, upto, "compaction", [{"type": "text", "text": summary}], model=model,
                                 meta={"reason": reason, "tokens_before": tokens_before,
                                       "tokens_after": context.estimate_tokens([{"content": summary}])})
        if before:
            store.reparent(before, node["id"])
            store.update_conversation(conv_id, leaf_id=conv["leaf_id"])
        self.app.stats.event("compaction", account["id"], conv_id, window, reason=reason, tokens_before=tokens_before,
                             tokens_after=node["meta"]["tokens_after"], model=model)
        self._pub(account["id"], "conv.reload", {"conv_id": conv_id})
        return node

    async def _title(self, account: dict, conv_id: str, model: str) -> None:
        store = self.app.stores.get(account["id"])
        thread = store.thread(conv_id)
        first = next((m for m in thread if m["role"] == "user"), None)
        if not first:
            return
        text = " ".join(b.get("text", "") for b in first["blocks"] if b.get("type") == "text") or "(attachment)"
        caps = (self.app.registry.get(model) or {}).get("info", {}).get("capabilities") or []
        try:
            res = await self.app.gateway.complete(
                model=model, kind="title", messages=[{"role": "user", "content": prompts.TITLE.format(text=clip(text, 1500))}],
                think=False if "thinking" in caps else None, options={"num_predict": 24, "temperature": 0.3},
                account_id=account["id"], conv_id=conv_id, label="naming the conversation")
        except (OllamaError, ResidencyError, ModelNotAllowed):
            return
        title = re.sub(r"[\"'`*#$]", "", res["content"]).strip().split("\n")[0][:80].rstrip(".")
        if not title or re.search(r"[\\{}^_<>|]", title) or len(title.split()) > 12:
            words = re.sub(r"\s+", " ", text).strip().split(" ")
            title = " ".join(words[:7]) + ("…" if len(words) > 7 else "")
        if title and not store.conversation(conv_id)["title"]:
            store.update_conversation(conv_id, title=title)
            self._pub(account["id"], "conv", {"conversation": store.conversation(conv_id)})

    async def rewind(self, account: dict, conv_id: str, user_msg_id: str, files: bool, conversation: bool) -> dict:
        store = self.app.stores.get(account["id"])
        msg = store.message(user_msg_id)
        if msg is None or msg["conv_id"] != conv_id or msg["role"] != "user":
            raise KeyError(user_msg_id)
        if conv_id in self.turns:
            raise RuntimeError("Stop the running reply first")
        result = {"files": None}
        conv = store.conversation(conv_id)
        if files and conv["folder"]:
            thread = store.path(conv["leaf_id"])
            ids = [m["id"] for m in thread]
            later = ids[ids.index(user_msg_id):] if user_msg_id in ids else [user_msg_id]
            ck = None
            for mid in later:
                ck = store.checkpoint_for(mid)
                if ck:
                    break
            if ck:
                result["files"] = await asyncio.to_thread(self.app.checkpoints.restore, account["id"], ck["folder"], ck["manifest"])
            else:
                result["files"] = {"restored": [], "removed": [], "note": "No file changes were made after that message."}
        if conversation:
            store.update_conversation(conv_id, leaf_id=msg["parent_id"])
            result["text"] = " ".join(b.get("text", "") for b in msg["blocks"] if b.get("type") == "text")
        self.app.stats.event("rewind", account["id"], conv_id, None, files=files, conversation=conversation)
        self._pub(account["id"], "conv.reload", {"conv_id": conv_id})
        return result


def suggest_rule(name: str, args: dict, folder: str | None) -> str | None:
    if name == "bash":
        words = (args.get("command") or "").split()
        if not words:
            return None
        prefix = " ".join(words[:2]) if len(words) > 1 and not words[1].startswith(("-", "/", ".")) else words[0]
        return f"Bash({prefix}:*)"
    if name in ("write_file", "edit_file"):
        path = args.get("path") or ""
        d = os.path.dirname(path.rstrip("/")) if path else ""
        if folder and os.path.isabs(d):
            d = os.path.relpath(d, folder)
        return f"Edit({d}/**)" if d and d != "." else "Edit(**)"
    if name == "web_fetch":
        host = re.sub(r"^[a-z]+://", "", (args.get("url") or "").lower()).split("/", 1)[0].split(":")[0]
        return f"WebFetch(domain:{host})" if host else None
    if name in ("read_file", "list_files", "search"):
        return None
    return name


ARTIFACT_TOOLS = {"create_artifact", "update_artifact", "rewrite_artifact"}
VISIBLE_TOOLS = ARTIFACT_TOOLS | {"write_file", "edit_file", "create_file", "generate_image"}


def _function(call: dict) -> str:
    return (call.get("function") or {}).get("name") or ""


_UNREADABLE = re.compile(r"XML syntax error|error parsing tool call|failed to parse tool call|"
                         r"unexpected end of JSON input|invalid character .{1,40} looking for", re.I)


def tool_call_unreadable(message: str) -> bool:
    """True for the errors Ollama ends a reply with when it cannot read the tool call the model wrote."""
    return bool(_UNREADABLE.search(message or ""))


_CALL_TEXT = re.compile(r'"(?:name|tool|tool_name|function|action)"\s*:\s*"(\w+)"|^\s*(\w+)\(\s*\{', re.M)
_ARG_KEYS = ("arguments", "parameters", "args", "input")


def _looks_like_tool_call(text: str, tools: dict) -> bool:
    """A tool call written as text instead of made: tool-call tags, `name({...})`, or JSON naming one of the
    `tools` ({name: Tool}) beside its arguments. Any length: a long reply can hide one, and invent its results."""
    if not text:
        return False
    if "<tool_call>" in text or "<function=" in text:
        return True
    for m in _CALL_TEXT.finditer(text):
        tool = tools.get(m.group(1) or m.group(2))
        if tool is None:
            continue
        if m.group(2):
            return True
        near = text[max(0, m.start() - 300):m.end() + 400]
        if any(f'"{k}"' in near for k in (*_ARG_KEYS, *tool.params)):
            return True
    # a fenced JSON object holding exactly a tool's arguments, without its name (llm-jp-4.1, 2026-10-02)
    for block in re.findall(r"^```(?:json)?[ \t]*\n(\{.*?\})\s*\n```", text, re.M | re.S):
        try:
            obj = json.loads(block)
        except ValueError:
            continue
        if isinstance(obj, dict) and any(len(t.required) >= 2 and set(t.required) <= set(obj) for t in tools.values()):
            return True
    return False


_DONE = (r"(?:created|re-?generated|rewritten|rewrote|updated|saved|fixed|revised|generated|built|made|written|wrote|"
         r"added|removed|changed|replaced|applied|stored|implemented|cleaned|refreshed|redone|redid|uploaded|placed)")
_CLAIM = re.compile(
    rf"\b(?:(?:i|we)(?:'ve|\s+have)\s+(?:now\s+|just\s+|already\s+|successfully\s+)?{_DONE}"
    rf"|(?:i|we)\s+(?:just\s+|now\s+|already\s+|successfully\s+)?{_DONE}"
    r"|(?:i|we)\s+\*{0,2}did\*{0,2}\s+(?:create|make|build|write|update|fix|save|regenerate|rewrite|change|add|remove)"
    rf"|(?:has|have)\s+(?:now\s+)?been\s+(?:now\s+|successfully\s+|fully\s+)?{_DONE}"
    r"|(?:is|are)\s+now\s+(?:saved|stored|updated|fixed|ready|available|live))\b", re.I)
# things that live outside the reply (an artifact, a file, an image), so saying they were made needs a tool call
_DELIVERABLE = re.compile(r"\b(?:artifact|page|landing|file|document|html|css|site|website|slide|deck|svg|image|"
                          r"spreadsheet|workbook|chart|diagram)", re.I)
_INLINE = re.compile(r"\b(?:below|above|the following)\b|```", re.I)  # the work is in the reply itself
_NOT_YET = re.compile(r"\b(?:i'll|i\s+will|we'll|we\s+will|let\s+me|let's|going\s+to|i'd|(?:i|we)\s+(?:would|could|can)|"
                      r"(?:should|shall)\s+(?:i|we)|if\s+you\s+(?:want|like|approve)|once\s+you)\b", re.I)
_ASK = re.compile(r"\b(?:make|create|build|write|fix|change|update|rewrite|revise|remove|add|rid|redo|regenerate|do|did|"
                  r"doing|done|again|show|put|turn|convert|apply|replace|delete|edit|improve|clean|give|need|want|"
                  r"generate|design|draft|save|try|implement|refactor|format)\b", re.I)


def claims_done(text: str) -> bool:
    """True when a reply says outright that it made or changed something (a page, a file, an artifact): a
    sentence saying so, with the thing named in it or in the next one."""
    if re.search(r"^(`{3,}|~{3,})", text or "", re.M):
        return False  # it delivered code or a page in the reply
    sentences = [x for x in re.split(r"(?<=[.!?:])\s+|\n+", text or "") if x.strip()]
    for i, sentence in enumerate(sentences):
        if _INLINE.search(sentence):
            continue
        if _CLAIM.search(sentence) and not _NOT_YET.search(sentence) \
                and _DELIVERABLE.search(" ".join(sentences[i:i + 2])):
            return True
    return False


def asks_for_work(text: str) -> bool:
    """True when a message asks for something to be made, changed or done (rather than only asking a question)."""
    return bool(_ASK.search(text or ""))


def _made_something(blocks: list, visible: bool = False) -> bool:
    """True when this turn made or changed something: an artifact, file or image (`visible`), or also any other
    change (a memory, the task list, a command that changed files)."""
    for b in blocks:
        if b.get("type") == "text" and b.get("artifacts"):
            return True
        if b.get("type") == "image" and b.get("status") == "done":
            return True
        if b.get("type") != "tool" or b.get("status") != "done":
            continue
        name = b.get("name")
        if name in VISIBLE_TOOLS or b.get("files"):
            return True
        if visible:
            continue
        if name in ("memory", "todo_write"):
            return True
        if name == "bash" and b.get("exit_code") == 0:
            from .shellcheck import classify
            if classify((b.get("args") or {}).get("command") or "", None).kind != "safe":
                return True
    return False


_FENCE = re.compile(r"^(?P<fence>`{3,}|~{3,})[ \t]*(?P<info>[^\n`]*)\n(?P<body>.*?)\n(?P=fence)[ \t]*$", re.M | re.S)


def page_fences(text: str) -> list[dict]:
    """The complete web pages and SVG images written as fenced blocks in `text`:
    [{start, end, kind, content, title}], `title` from the fence (```html title="…"), when given."""
    out = []
    for m in _FENCE.finditer(text or ""):
        info = m.group("info").strip()
        lang = info.split()[0].lower() if info else ""
        body = m.group("body")
        head = body.lstrip()[:300].lower()
        if lang in ("html", "htm", "") and head.startswith(("<!doctype html", "<html")):
            kind = "html"
        elif lang in ("html", "htm") and body.count("\n") >= 40:
            kind = "html"
        elif (lang in ("svg", "xml", "") or (lang == "html" and head.startswith("<svg"))) and "<svg" in head \
                and "</svg>" in body.lower():
            kind = "svg"
        else:
            continue
        title = re.search(r'title\s*=\s*["\']([^"\']+)["\']', info)
        out.append({"start": m.start(), "end": m.end(), "kind": kind, "content": body.strip("\n"),
                    "title": title.group(1).strip() if title else None})
    return out


def page_title(content: str) -> str | None:
    import html
    m = re.search(r"<title[^>]*>(.*?)</title>", content or "", re.I | re.S)
    t = html.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""
    return t[:120] or None


def _repeating_failures(blocks: list, n: int = 3) -> bool:
    """True when the last n tool calls were the same call and all failed or were refused."""
    tools = [b for b in blocks if b.get("type") == "tool"][-n:]
    if len(tools) < n or any(b.get("status") not in ("error", "denied") for b in tools):
        return False
    keys = {(b.get("name"), json.dumps(b.get("args"), sort_keys=True, default=str)) for b in tools}
    return len(keys) == 1


def time_stamp() -> str:
    import time as _t
    return _t.strftime("%Y%m%d-%H%M%S")


def _snapshot(root: str) -> dict[str, tuple[int, int]]:
    """{relative path: (size, mtime_ns)} of the files in a scratch folder (uploads excluded)."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == "uploads" or rel_dir.startswith("uploads" + os.sep):
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "__pycache__"]
        for f in filenames:
            if f.startswith("."):
                continue
            p = os.path.join(dirpath, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
            if len(out) > 2000:
                return out
    return out


def _changed_files(before: dict, after: dict) -> list[dict]:
    return [{"path": p, "name": os.path.basename(p), "size": after[p][0]}
            for p in sorted(after) if before.get(p) != after[p]][:50]


def _sources(res: dict) -> list[dict]:
    return [{"file": r["name"], "part": r["seq"] + 1, "file_id": r["file_id"]} for r in res.get("results") or []]


def _short_args(args) -> str:
    if not isinstance(args, dict):
        return clip(str(args), 120)
    for key in ("command", "path", "pattern", "query", "url"):
        if key in args:
            return clip(str(args[key]), 120)
    return clip(json.dumps(args, ensure_ascii=False), 120)

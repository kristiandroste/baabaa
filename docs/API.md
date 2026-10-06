# HTTP API

Everything the browser and terminal do goes through this API, so scripts can do it too.

**Authentication.** Create a key in **Settings → General → API keys** and send it as
`Authorization: Bearer bk_…`. A key acts as its account (owner endpoints need an owner's key), needs no
CSRF token, and can be revoked at any time. The browser uses a session cookie plus an `X-CSRF-Token`
header instead. Requests are accepted only from the local network and must use one of the host's names.

All bodies are JSON. Errors are `{"error": "…"}` with a 4xx or 5xx status.

## Conversations

| Method and path | Body / query | Result |
|---|---|---|
| `GET /api/conversations` | `q`, `limit`, `before`, `folders=1`, `archived=1` | `{conversations}` |
| `POST /api/conversations` | `{title?, model?, mode?, folder?, incognito?, think?, network?, project_id?}` | `{conversation}` |
| `GET /api/conversations/{id}` | | `{conversation, thread, state, artifacts, context, trusted, shells}` |
| `PATCH /api/conversations/{id}` | `{title?, pinned?, archived?, model?, mode?, folder?, settings?: {think, network, style}}` | `{conversation}` |
| `DELETE /api/conversations/{id}` | | |
| `POST /api/conversations/{id}/messages` | `{text, attachments?: [id], parent_id?, edit?, research?}` | `{message}` or `{queued: true}` |
| `POST /api/conversations/{id}/stop` | | `{stopped}` |
| `POST /api/conversations/{id}/regenerate` | `{message_id}` (an assistant message) | |
| `POST /api/conversations/{id}/branch` | `{message_id}` (show that version) | `{conversation, thread}` |
| `POST /api/conversations/{id}/compact` | | `{message}` (the summary) |
| `POST /api/conversations/{id}/rewind` | `{message_id, files, conversation}` | `{files, text}` |
| `POST /api/conversations/{id}/fork` | `{message_id?}` | `{conversation}` |
| `POST /api/conversations/{id}/feedback` | `{message_id, rating: -1, 0 or 1, note?}` | |
| `GET /api/conversations/{id}/export` | `format=md|html|json`, `scope=branch|all` | a file |
| `GET /api/conversations/{id}/files` | `path=` (a file in the chat's folder or the working folder) | the file, or `{files}` for a chat's folder |
| `GET /api/search` | `q` | `{results}` |
| `POST /api/pending/{id}` | approval `{decision: allow_once|allow_always|deny, rule?, reason?}`; question `{text}`; plan `{decision: approve|revise, mode?, text?}` | |

A message's `blocks` are `text`, `thinking` (with `ms`, how long the thought took, once it has ended), `tool`
(name, args, status, output, decision, judge, diff), `attachment`, `notice` and `error`. A reply is one assistant
message holding the whole turn.

`research: true` (or a message starting with `/research `) answers from the web: the reply's blocks show
each step (`research_plan`, `web_search`, `web_fetch`, `research_notes`) and end with the cited report.

`image: {shape?: square|portrait|landscape|wide, edit?}` makes the message an image request (the Image
switch): the reply holds an `image` block (`id` of the saved PNG attachment, `prompt`, `model`, `width`,
`height`, `seed`, `seconds`); an attached image, or `edit: true`, changes that or the latest image.
`POST /api/transcribe?lang=…` with WAV or MP3 as the body → `{text, model}` (dictation).
`GET /api/images` → `{images}` (every generated image, newest first).

## Scheduling, sharing, worktrees

| Method and path | Body / query | Result |
|---|---|---|
| `GET /api/schedules` | | `{schedules, timezone}` (each with `when`, `next_ms`, `last_conv`) |
| `POST /api/schedules` | `{title, prompt, spec: {kind: once|daily|weekdays|weekly|interval, at, date?, days?, every_min?}, settings?: {research, same_conversation, project_id, model, think}}` | `{schedule}` |
| `PATCH /api/schedules/{id}` | any of the above, `enabled` | `{schedule}` |
| `DELETE /api/schedules/{id}`, `POST /api/schedules/{id}/run` | | run now: `{schedule}` with `last_conv` |
| `POST /api/conversations/{id}/share` | `{to: [account ids] or "*"}` | `{share}` (a read-only snapshot) |
| `GET /api/shares` | | `{shared_with_me, shared_by_me}` |
| `GET /api/shares/{id}`, `POST /api/shares/{id}/copy`, `DELETE /api/shares/{id}` | | read / continue as your own copy / stop sharing |
| `POST /api/conversations/{id}/worktree` | `{name?}` | `{conversation, branch, path}`: the conversation moves to a new git worktree |
| `DELETE /api/conversations/{id}/worktree` | `force=1` to drop uncommitted work | back to the original folder; the branch stays |

## Projects and memory

| Method and path | Body / query | Result |
|---|---|---|
| `GET /api/projects` | `archived=1` | `{projects}` (with counts) |
| `POST /api/projects` | `{name, description?, instructions?}` | `{project}` |
| `GET /api/projects/{id}` | | `{project, files, conversations, knowledge_mode, embed_model}` |
| `PATCH /api/projects/{id}` | `{name?, description?, instructions?, archived?}` | `{project}` |
| `DELETE /api/projects/{id}` | | the project, its documents and memory; its conversations stay |
| `POST /api/projects/{id}/files?name=…` | the raw file | `{file}` (text is extracted; indexing runs in the background) |
| `POST /api/projects/{id}/text` | `{name, text}` | `{file}` |
| `GET /api/projects/{id}/files/{fid}` | `download=1` for the original | `{file}` with its text |
| `DELETE /api/projects/{id}/files/{fid}` | | |
| `GET /api/projects/{id}/search` | `q`, `k` | `{results, method: hybrid|keyword, note}` |
| `GET /api/memory` | | `{memories, enabled, chat_search}` |
| `POST /api/memory` | `{text, project_id?}` | `{memory}` |
| `PATCH /api/memory/{id}`, `DELETE /api/memory/{id}` | `{text}` | |

`knowledge_mode` is `full` (documents go into the prompt whole) or `search` (passages are retrieved for
each message). Conversations join a project with `project_id` on creation or `PATCH`.

## Live events

`GET /api/events` is a Server-Sent Events stream for the account. Event names: `msg.new`, `msg.delta`
(`{msg_id, index, text, len}`: append `text` to block `index`; `len` is the block's length afterwards),
`msg.block`, `msg.done`, `turn` (running, waiting, idle; `queue_position`, sent again whenever the reply's place
in the GPU queue changes), `pending` and `pending.done`
(approvals, questions, plans), `context`, `todos`, `artifact`, `tool.output` (live command output), `rules`,
`conv`, `conv.reload`, `conv.deleted`, `queue` (the GPU queue), `job` (installs, fit tests, model
searches), `models.updated`, `notice`, `project` and `project.file` (documents and their indexing
progress: `done` of `total` chunks), `memory`, `schedule`, `share`, `image.progress` (`{msg_id, index,
status, seconds}` while an image is made), `update` (`{state, target, busy}`: fetch `GET /api/update`) and
`restart` (`{pending, error}`; `pending.phase` is `waiting` or `restarting`). The first event, `hello`,
carries the server's `version`: a window that loaded another version reloads.

## Updates and restarts

`GET /api/update` → `{kind (installed|git|folder), running, installed, state, target, latest {version, date,
notes}, channel, checks, checked_ms, error, busy (checking|downloading), previous (owners), can_act, owner,
restart_by (all|owners), pending, restart_error, work {replies, yours, jobs}, target_notes, running_notes}`.
`state` is `up_to_date`, `available` (a release to download: Install), `ready` (downloaded and checked),
`installed` (installed but not running: Restart) or `changed` (a checkout's files changed since the server
started). `POST /api/update/install` (`{now?}`) downloads if needed, switches and restarts;
`POST /api/restart` (`{now?}`) and `DELETE /api/restart` start and cancel a restart. These three need
`can_act`: every account, or owners only (`restart_by`). Owners: `POST /api/update/check`,
`POST /api/update/previous` (`{now?}`), `PATCH /api/update/settings` (`{channel?, checks?, restart_by?}`).
A restart waits for running replies and model jobs unless `now`; meanwhile new replies get 409.
The terminal's `POST /api/local/restart` (`{now?, reason?}`) answers only on the Unix socket, with the
local key.

Who can connect (owners): `GET /api/network` → `{saved (local|lan), running, urls, next_urls (where baabaa
will be after a restart, when the saved mode differs), passwordless (accounts anyone on the network could open),
remote (this request comes from another device)}`; `PATCH /api/network` (`{mode}`). A restart while they differ
has reason `network` and carries `next_url`, which windows follow.

## Files, artifacts, rules, customization

`POST /api/uploads?name=…&conv_id=…` with the raw file as the body → `{attachment}`;
`GET /api/attachments/{id}`; `GET /api/artifacts?conv_id=…`; `GET /api/artifacts/{id}?version=…`; `GET /api/artifacts/{id}/export?format=docx|pdf|pptx` (documents and slides);
`GET|POST /api/rules`, `DELETE /api/rules/{id}`; `GET /api/extend`,
`GET|PUT|DELETE /api/extend/{skills|commands|agents|styles|hooks}/{name}`;
`GET|POST /api/connectors`, `PATCH|DELETE /api/connectors/{id}`, `POST /api/connectors/{id}/test`.
Adding a connector that runs a program here (`transport: stdio`), like registering a llama.cpp model,
needs an owner account with a password and that `password` in the body (or the local terminal).

## Models, statistics, accounts (owner)

`GET /api/models` (everyone: approved models, each with `tool_use` `{passed, of, checks}` from its fit test
when tested; owners also: installed models, fit results, jobs);
`POST /api/models/sync|test|approve|pull|remove`; `POST /api/models/scout` (find models: `{model?, other_families?}`
for newer versions of one installed model or all of them, or `{query?, category?}` to search the library; one
runs at a time, a different request replaces it); `POST /api/models/scout/dismiss` (`{key}` hides a suggestion
for good, `{clear: job_id}` clears a finished search; returns `{dismissed, cleared, categories}`, also in
`GET /api/models` as `scout`); `POST /api/models/external` (register a model served
by another program: `{runtime: llamacpp, name, server, model, lib_dirs?, args?, mmproj?, password}` or
`{runtime: sdcpp, name, server, diffusion_model, llm?, vae?, llm_vision?, edit?, staged?, steps?, cfg?, …}`); `POST /api/gpu/pause`
(`{paused, reason?}`); `GET /api/jobs`; `POST /api/jobs/{id}/cancel`;
`GET|PATCH /api/settings`. Statistics: `GET /api/stats/summary?group=day|model|account_id|kind`,
`GET /api/stats/gpu`, `GET /api/stats/export?table=…&format=csv|jsonl`, `GET /api/stats/snapshot`
(see [STATISTICS.md](STATISTICS.md)). Accounts: `GET|POST /api/accounts`, `PATCH|DELETE /api/accounts/{id}`,
`POST|DELETE /api/accounts/{id}/grants`.

## Example

```sh
KEY=bk_…; H="Authorization: Bearer $KEY"; B=https://myhost.local:8443
CID=$(curl -s -H "$H" -X POST $B/api/conversations -d '{}' | python3 -c 'import sys,json; print(json.load(sys.stdin)["conversation"]["id"])')
curl -s -H "$H" -X POST $B/api/conversations/$CID/messages -d '{"text": "Hello"}'
curl -sN -H "$H" $B/api/events      # watch the reply stream in
```

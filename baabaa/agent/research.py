"""Research: a question answered from the web with sources, as a fixed pipeline small models can follow.

1. plan  - the model writes a few search queries (JSON);
2. search - every query goes to the web search, at once (network only, no GPU);
3. choose - the sources that rank well across queries, a handful at most;
4. read  - each page is fetched and turned into text;
5. notes - the model reads each page alone and notes what bears on the question;
6. write - the model writes the report from the notes, citing [1], [2] … and listing the sources.
Every step shows up in the reply as it happens, like tool calls, and Stop ends it at once.
"""

import asyncio
import time

from ..util import clip, extract_json, new_id
from . import permissions, web

MAX_QUERIES = 5
MAX_SOURCES = 6
PAGE_CHARS = 9000

PLAN = """You are planning web research to answer the question below well.
Write {n} different web search queries that together cover it: the main question, the specific facts or
numbers it needs, and a recent or authoritative angle. Short queries, like a person types them.
Reply with JSON only: {{"queries": ["...", "..."]}}

Question: {q}"""

NOTES = """Question: {q}

Below is the text of one web page (source [{i}]: {title}). Write down, as short bullet points, every fact
from it that helps answer the question: numbers, dates, names, findings, quotes, and caveats. Keep the
page's own wording for numbers. If nothing on the page is relevant, reply exactly: NOTHING RELEVANT.

<page>
{text}
</page>"""

REPORT = """Write a research report that answers the question below, using ONLY the notes from the numbered
sources. Structure: a direct answer first (two or three sentences), then the details under short
headings, then any disagreements between sources or open questions. After each fact, cite its source
like [1] or [2][3]. Do not invent sources or facts that are not in the notes. End with a "Sources"
section listing each source you cited as: [n] title - URL.

Question: {q}

Notes:
{notes}"""


def pick_sources(results: list[list[dict]], limit: int = MAX_SOURCES) -> list[dict]:
    """Sources that rank high across the queries: reciprocal-rank score, one per site where possible."""
    score, first = {}, {}
    for ranking in results:
        for rank, r in enumerate(ranking):
            url = r["url"].split("#")[0]
            score[url] = score.get(url, 0.0) + 1.0 / (3 + rank)
            first.setdefault(url, r)
    ordered = sorted(score, key=lambda u: -score[u])
    chosen, sites = [], set()
    for url in ordered:  # prefer different sites first
        site = url.split("/")[2] if "//" in url else url
        if site not in sites:
            chosen.append(first[url])
            sites.add(site)
        if len(chosen) >= limit:
            return chosen
    for url in ordered:
        if first[url] not in chosen:
            chosen.append(first[url])
        if len(chosen) >= limit:
            break
    return chosen


async def run(agent, turn, store, model: str, caps: list, blocks: list, question: str) -> None:
    app = agent.app
    think = False if "thinking" in caps else None
    t0 = time.monotonic()
    conv = store.conversation(turn.conv_id)
    ctx = agent._context(turn, conv)

    def step(name: str, args: dict, status: str = "running", output: str = "") -> dict:
        b = {"type": "tool", "id": new_id(), "name": name, "args": args, "status": status, "output": output,
             "research": True}
        blocks.append(b)
        agent._block(turn, blocks, b)
        return b

    def done(b: dict, output: str, status: str = "done", **extra) -> None:
        b.update({"status": status, "output": output, **extra})
        agent._block(turn, blocks, b)

    async def ask(prompt: str, label: str, predict: int, fmt=None) -> str:
        res = await app.gateway.complete(
            model=model, messages=[{"role": "user", "content": prompt}], think=think, fmt=fmt, kind="research",
            options={"temperature": 0.2, "num_predict": predict}, account_id=turn.account["id"], conv_id=turn.conv_id,
            msg_id=turn.assistant_id, window=turn.window, label=label)
        return res["content"]

    # 1. plan
    plan = step("research_plan", {"question": clip(question, 300)})
    schema = {"type": "object", "properties": {"queries": {"type": "array", "items": {"type": "string"}}}, "required": ["queries"]}
    raw = await ask(PLAN.format(q=question, n=MAX_QUERIES - 1), "research: planning", 300, schema)
    data = extract_json(raw) or {}
    queries = [clip(str(q).strip(), 200) for q in (data.get("queries") or []) if str(q).strip()][:MAX_QUERIES]
    if not queries:
        queries = [clip(question, 200)]
    done(plan, "\n".join(f"- {q}" for q in queries), queries=queries)

    # 2. search, all at once
    searches = [step("web_search", {"query": q}) for q in queries]

    async def one_search(q):
        try:
            return await web.search(q, 8)
        except web.FetchError as exc:
            return exc
    found = await asyncio.gather(*(one_search(q) for q in queries))
    rankings = []
    for b, res in zip(searches, found):
        if isinstance(res, Exception):
            done(b, f"Error: {res}", "error")
            continue
        rankings.append(res)
        done(b, "\n\n".join(f"{i}. {r['title']}\n   {r['url']}" for i, r in enumerate(res, 1)) or "[no results]")

    # 3. choose, honouring the account's WebFetch rules
    sources = []
    for r in pick_sources(rankings, MAX_SOURCES * 2):
        d = permissions.decide("web_fetch", "net", {"url": r["url"]}, ctx)
        if d.action != "deny":
            sources.append(r)
        if len(sources) >= MAX_SOURCES:
            break
    if not sources:
        blocks.append({"type": "notice", "text": "The web search found nothing to read, so there is no report."})
        agent._block(turn, blocks, blocks[-1])
        return

    # 4. read, four at a time
    reads = [step("web_fetch", {"url": s["url"]}) for s in sources]
    sem = asyncio.Semaphore(4)

    async def one_fetch(s):
        async with sem:
            try:
                return await web.fetch(s["url"], PAGE_CHARS)
            except web.FetchError as exc:
                return exc
    pages = await asyncio.gather(*(one_fetch(s) for s in sources))
    usable = []
    for b, s, page in zip(reads, sources, pages):
        if isinstance(page, Exception) or len((page.get("content") or "").strip()) < 200:
            done(b, f"Could not read it: {page}" if isinstance(page, Exception) else "Too little text on the page.", "error")
            continue
        title = page.get("title") or s.get("title") or s["url"]
        done(b, f"{title}\n{len(page['content']):,} characters read")
        usable.append({"url": page.get("url") or s["url"], "title": clip(" ".join(title.split()), 160), "text": page["content"]})

    # 5. notes, one page at a time on the GPU
    notes = []
    for i, src in enumerate(usable, 1):
        b = step("research_notes", {"source": i, "title": src["title"], "url": src["url"]})
        text = (await ask(NOTES.format(q=question, i=i, title=src["title"], text=src["text"]),
                          f"research: reading source {i}", 700)).strip()
        relevant = text and "NOTHING RELEVANT" not in text.upper()[:40]
        done(b, text or "(no notes)", "done" if relevant else "error")
        if relevant:
            notes.append((i, src, text))
    if not notes:
        blocks.append({"type": "notice", "text": "None of the pages read had anything on the question."})
        agent._block(turn, blocks, blocks[-1])
        return

    # 6. the report, streamed like any reply
    note_text = "\n\n".join(f"[{i}] {src['title']} - {src['url']}\n{clip(text, 3000)}" for i, src, text in notes)
    text_block = {"type": "text", "text": ""}
    blocks.append(text_block)
    agent._block(turn, blocks, text_block)
    async for chunk in app.gateway.chat(
            model=model, messages=[{"role": "user", "content": REPORT.format(q=question, notes=note_text)}], think=think,
            kind="research", options={"temperature": 0.3}, account_id=turn.account["id"], conv_id=turn.conv_id,
            msg_id=turn.assistant_id, window=turn.window, label="research: writing"):
        piece = (chunk.get("message") or {}).get("content")
        if piece:
            text_block["text"] += piece
            agent._delta(turn, blocks, text_block, piece)
    agent._flush(turn, blocks)
    app.stats.event("research", turn.account["id"], turn.conv_id, turn.window, queries=len(queries),
                    sources_read=len(usable), sources_used=len(notes), seconds=round(time.monotonic() - t0, 1))


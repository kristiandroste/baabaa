# Usage statistics

baabaa records what happens as event-level metadata in one SQLite database, `stats.db`. It never
records content: no message text, prompts, file contents or command output.

- Timestamps (`ts_ms`) are UTC, milliseconds since the Unix epoch.
- Durations (`*_ms`) are milliseconds, measured with a monotonic clock or taken from Ollama.
- Energy (`energy_mj`) is millijoules, from the GPU driver's cumulative energy counter, read before and
  after each request. It is approximate when other programs use the GPU at the same time.
- Every table except `gpu_samples` has `account_id`. An account sees its own rows; owners see all.
- The schema version is `PRAGMA user_version`; new versions only add tables and columns.

Get the data: **Settings → Usage → Export** (CSV or JSON Lines per table, or the whole database),
`GET /api/stats/export?table=…&format=csv|jsonl&from=…&to=…`, or
`baabaa stats export TABLE --format csv --days 30 --out file.csv` on the host.

## `model_requests` — one row per call to a model

| Column | Meaning |
|---|---|
| `id`, `ts_ms` | Row id; when the request ended |
| `account_id`, `conv_id`, `msg_id` | Who, in which conversation, for which reply |
| `window` | `browser`, `terminal` or empty for background work |
| `kind` | `chat`, `judge` (auto-mode check), `title`, `compact`, `subagent`, `research` (planning, notes and report of a research turn), `image` (an image made by an image model), `transcribe` (dictation), `scout` (finding models: newer versions or a library search), `fit` (GPU fit test), `embed` |
| `model`, `num_ctx`, `think` | Model, its context size, the thinking setting |
| `queue_wait_ms` | Time waiting for the GPU queue |
| `load_ms` | Time to load the model into GPU memory (0 or empty when already loaded) |
| `ttft_ms` | Time from model ready to the first streamed chunk (prompt processing) |
| `prompt_tokens`, `output_tokens` | From Ollama (`prompt_eval_count`, `eval_count`); for llama.cpp models, the server's `timings` (cached prompt tokens included) |
| `prompt_eval_ms`, `eval_ms`, `total_ms` | Ollama's own durations |
| `wall_ms` | Wall-clock time from GPU slot to end |
| `tokens_per_s` | `output_tokens / eval_ms` |
| `outcome` | `ok`, `stopped` (by a person), `error`, `residency` (refused: not fully on the GPU), `cpu` (stopped by the CPU guard: model work on the CPU), `length`, `incomplete` |
| `error` | Short error text |
| `residency_ok`, `model_bytes`, `vram_bytes` | The residency check: model size and the part in VRAM (always equal when it ran) |
| `energy_mj` | GPU energy during the request |
| `cpu_s` | CPU seconds the program serving the request used during it (the CPU guard's reading; from schema version 2) |
| `cpu_peak` | its busiest moment, in cores (about 1 when the model is on the GPU) |
| `gaps` | JSON array: milliseconds between streamed chunks (token timing) |

## `gpu_samples` — one row per second while any model request runs

`ts_ms`, `vram_used` (bytes), `util_gpu`, `util_mem` (percent), `power_mw`, `temp_c`.

## `tool_calls` — one row per tool the agent called

`tool`, `mode` (manual, accept_edits, plan, auto), `decision` (allow, ask, deny), `layer` (what decided:
`safe`, `rule`, `mode`, `shell`, `path`, `judge`), `duration_ms`, `exit_code` (commands), `output_bytes`,
`sandbox_denied`, `error`.

## `approvals` — one row per action held for a person

`tool`, `mode`, `layer` (why it was held), the judge's view when it ran (`judge_risk`,
`judge_user_asked`, `judge_decision`), the person's answer (`final`: `allow_once`, `allow_always`,
`deny`, or empty when the turn was stopped), `approver_id`, `latency_ms`. Comparing `judge_decision`
with `final` measures how well Auto mode matches its users.

## `events` — everything else

`type` and a JSON `data` object: `message` (chars, attachments, edit), `turn` (status, tools,
duration_ms, model), `conversation_created`, `conversation_deleted`, `model_switched` (frm, to),
`compaction` (reason, tokens_before, tokens_after), `rewind`, `fork`, `branch_switched`, `regenerate`,
`stop`, `export` (format), `upload` (kind, mime, bytes, extracted_chars), `artifact` (action, kind, chars;
source `reply` for a page the model wrote in its reply),
`search` (terms), `login`, `login_failed`, `window_open`, `window_close`, `model_approved`,
`model_removed`, `model_registered` (runtime), `account_created`, `stats_export`, `gpu_paused`, `gpu_resumed`,
`ollama_stuck` (model, attempt: Ollama stopped answering for a model and baabaa unloaded it), `recovery`
(kind, model: baabaa sent the model back to work; kind `parse_error` when Ollama could not read its tool call,
`text_call` for a tool call written as text, `empty_reply` for a turn ending without a reply,
`unfinished_claim` for a reply saying something was made or changed when no tool ran).

Updates and restarts: `update_check` (manual, ok, latest, running, staged, published: whether a release list
exists), `update_apply` and `update_rollback` (running, before, to), `restart_requested` (reason: update,
changed or restart; now, running, target; no account for the local terminal), `restart_cancelled`,
`restart_failed` (stage: the new code failed its start-up check), `network_mode` (mode: local or lan).

Knowledge and files: `project_created`, `project_deleted`, `project_file_added` (kind, mime, bytes,
chars), `knowledge_indexed` (project_id, chunks, chars, model), `knowledge_search` (semantic, keyword,
returned: result counts), `gpu_search` (rows, dim, k, outcome, queue_wait_ms, wall_ms, energy_mj: the
vector search kernel), `memory_add`, `memory_update`, `memory_delete` (by: user or model; project),
`past_chats` (mode: search or recent; results), `file_created` (kind, bytes), `files_created` (tool,
count, bytes, kinds), `file_downloaded` (bytes, ext), `research` (queries, sources_read, sources_used,
seconds), `image_generated` (model, width, height, edit, seconds, bytes), `dictation` (bytes, chars,
seconds, model), `schedule_created` (kind), `schedule_run` (manual, kind, research), `share_created`
(recipients, everyone, messages), `share_copied`, `worktree_created`.

## `jobs` — model installs, fit tests and model searches

`kind` (`pull`, `fit`, `scout`), `model`, `duration_ms`, `bytes` (download size), `outcome`, `data` (JSON:
result or error). A chat model's fit result includes `tools`: `{passed, of, checks, errors?}` from three
checks (it calls a tool when it must, answers without one when it should, and writes a small web page as
a tool argument that Ollama can read).

## Examples

```sql
-- speed and energy per model, last 7 days
SELECT model, COUNT(*) n, ROUND(AVG(tokens_per_s),1) tok_s, ROUND(SUM(energy_mj)/3.6e6,2) wh,
       ROUND(SUM(energy_mj)/1000.0/NULLIF(SUM(output_tokens),0),3) joules_per_token
FROM model_requests WHERE kind='chat' AND ts_ms > (strftime('%s','now')-7*86400)*1000 GROUP BY model;

-- how often the auto-mode judge agreed with people
SELECT judge_decision, final, COUNT(*) FROM approvals WHERE layer='judge' GROUP BY 1, 2;
```

```python
import json, pandas as pd
df = pd.read_csv("baabaa-model_requests.csv")
gaps = df.dropna(subset=["gaps"]).gaps.map(json.loads).explode().astype(float)
print(gaps.describe())   # distribution of time between streamed tokens
```

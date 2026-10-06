# Developer guide

How baabaa is put together, for people who want to change it. It names the files and functions to read;
it does not repeat what other documents cover: the [README](../README.md) (features and commands),
[API.md](API.md) (the HTTP API and live events), [AUTO_MODE.md](AUTO_MODE.md) (the judge),
[RUNTIMES.md](RUNTIMES.md) (GPU rules and model programs other than Ollama), [STATISTICS.md](STATISTICS.md)
(the statistics database) and [RELEASING.md](RELEASING.md) (making a release).

## Ground rules

- **No dependencies.** The Python standard library, and hand-written HTML, CSS and JavaScript. No third-party
  packages, no vendored libraries, no build step. There is no `pyproject.toml`, `package.json` or linter
  configuration, on purpose.
- **Models run entirely on the GPU.** No change may let model work run on the CPU ([RUNTIMES.md](RUNTIMES.md)).
- **Statistics record metadata, never content.**
- **The tests pass, and need no GPU.**

[CONTRIBUTING.md](../CONTRIBUTING.md) has the rest: how to propose a change, the contributor agreement, and
where to report security problems.

## Working on it

```sh
git clone https://github.com/kristiandroste/baabaa && cd baabaa
bin/baabaa serve                                   # the server, in this terminal, from this folder
python3 -m unittest discover -s tests              # Python tests
for t in tests/js/*.test.mjs; do node "$t"; done   # renderer tests (Node 18 or newer)
```

`bin/baabaa` runs the copy in its own folder. A checkout does not update itself. To try things without
touching your own data, give it another data folder and port: `BAABAA_HOME=/tmp/bb bin/baabaa serve --port 8444`.
Python changes need a restart of the server; changes to the web files only a reload of the page.

Python is 3.10 or newer, 4 spaces, double quotes, lines up to about 120 columns, type hints in the
`str | None` style, records as plain dicts. Every module opens with a docstring that says what it is for.
Comments say why, often with a measurement and its date. JavaScript is ES modules, 2 spaces, single quotes,
semicolons, no framework. Text shown to people is plain, complete sentences that say what happened and what
to do next, and the name is always lower-case `baabaa`.

## Map of the code

| Where | What |
|---|---|
| `baabaa/__main__.py` | The command line: `serve`, `chat`, `doctor`, `models`, `gpu`, `account`, `stats` and the installer's commands. |
| `baabaa/app.py` | `App`: every long-lived service, created once. |
| `baabaa/server/` | `http.py` (a small HTTP/1.1 server on asyncio), `api.py` (`Web`: every route), `lan.py` (which clients and host names are accepted), `tls.py` (the local certificate authority). |
| `baabaa/agent/` | `loop.py` (the agent loop: a turn), `tools.py` (tool definitions, file tools), `permissions.py` (modes and rules), `shellcheck.py` (is a command safe), `judge.py` (Auto mode's model check), `context.py` and `prompts.py` (what the model is sent), `shell.py` (commands, always sandboxed), `checkpoints.py` (rewind), `research.py`, `web.py`. |
| `baabaa/sandbox/` | The tool sandbox: Landlock and seccomp on Linux, Seatbelt on macOS, and its self-test. |
| `baabaa/gateway.py` | Every model request: one GPU queue, the residency check, the CPU guard, statistics. |
| `baabaa/ollama.py`, `llamacpp.py`, `sdcpp.py` | Clients for Ollama and for the model programs baabaa starts itself. |
| `baabaa/models.py`, `scout.py`, `library.py` | The model registry, the fit test, installs, finding models. |
| `baabaa/gpu.py`, `cpuwatch.py`, `cuda.py`, `gguf.py` | GPU readings, the CPU guard, vector search on the GPU, reading model files. |
| `baabaa/store.py`, `maindb.py`, `stats.py`, `db.py`, `paths.py` | Storage: one database per account, one for the machine, one for statistics. |
| `baabaa/accounts.py`, `events.py` | Accounts and sessions; the publish/subscribe bus behind live updates. |
| `baabaa/extend.py`, `mcp.py`, `scheduler.py`, `knowledge.py` | Skills, commands, agents, styles and hooks; connectors; scheduled tasks; project documents. |
| `baabaa/docs.py`, `writers/`, `export.py` | Reading uploaded documents; writing Word, Excel, PowerPoint and PDF; exports. |
| `baabaa/update.py`, `installer.py`, `restarter.py`, `signing.py`, `daemon.py` | Installing, updating, restarting in place, checking signatures, running in the background. |
| `baabaa/web/` | The browser app: `index.html`, `css/app.css`, `js/`. |
| `baabaa/tui/` | The terminal app: `client.py` (the transport), `app.py` (curses), `thread.py`. |
| `tools/` | `release.py` (releases) and `website.py` (the website and its preview). |
| `tests/` | Tests, with stand-ins for Ollama and the model servers in `tests/fixtures/`. |

## How the server starts

`baabaa serve` (`__main__.serve`):

1. Re-executes itself once with a clean environment, so that tool processes cannot read secrets from it.
2. Takes the lock for the data folder (`daemon.hold_lock`): one server per data folder.
3. Reads the saved network choice (`server/lan.saved_mode`) and builds a `LanGuard`: the addresses to listen
   on, and the clients and host names to accept.
4. Creates `App`, which builds the services in order: `paths`, `maindb`, `accounts`, `stats`, `gpu`, `ollama`,
   `events`, `registry`, `llamacpp`, `sdcpp`, `gateway`, `jobs`, `stores`, `shells`, `checkpoints`, `agent`,
   `mcp`, `extend`, `knowledge`, `scheduler`, `restarter`, `updates`. Services reach each other through
   `app.<name>`. `App.startup()` then reads Ollama's models and resumes what a restart interrupted.
5. Makes the TLS context when other devices may connect, creates `Web` and `Server`, and listens on TCP and
   on a Unix socket in the data folder. The terminal app uses the socket, and proves it runs as the same
   user with `local.key`, a secret that sandboxed tools cannot read.
6. Runs until stopped. A restart for an update replaces the process with `os.execve` (`restarter.py`).

## One message, from the browser to the reply

1. **Browser.** The composer posts to `/api/conversations/{id}/messages` (`web/js/views/chat.js`, through
   `web/js/api.js`, which adds the CSRF token).
2. **Checks** (`Web.__call__`, `_dispatch` and `_authenticate` in `server/api.py`), in this order: the client's
   address is allowed; the `Host` header is one of this machine's names (against DNS rebinding); the session
   cookie, or an API key, or the local key on the socket; for a request that changes something, a same-origin
   `Origin` and the CSRF token. Handlers raise plain exceptions, which `Web.__call__` maps to statuses
   (`ValueError` 400, `PermissionError` 403, `KeyError` 404, and so on).
3. **Agent** (`agent/loop.py`). `Agent.send` stores the user's message and starts a `Turn`: one per
   conversation, a task running `_run`. `_run` creates the assistant message with status `streaming`, then
   `_loop` repeats, up to 60 steps: choose the tools to offer, build the prompt (`prompts.system_prompt`,
   `context.build`, compaction when the conversation no longer fits), call the model, run the tools it asked
   for. The reply's blocks live in `Turn.blocks` and are written to the database once, at the end.
4. **Gateway** (`gateway.Gateway.chat`). Every model request waits in `GpuQueue` (one at a time, first come
   first served), is loaded and checked to be entirely in GPU memory before any prompt is processed
   (`ensure_loaded`), is watched by the CPU guard, and leaves one row in the statistics. A failed check raises
   `ResidencyError`.
5. **Events.** As text arrives, the loop publishes `msg.block` (a block started or changed) and `msg.delta`
   (text for a block, at most every 40 ms) on `EventBus`. Each open window holds one subscription and
   receives its account's events as Server-Sent Events from `GET /api/events`. A window that opens part-way
   gets the reply so far from `Agent.live_blocks`.
6. **Browser again.** `wireConversationEvents` in `chat.js` applies each event to the page, and `thread.js`
   draws the strand under the reply from the same events.

A conversation is a tree of messages: `parent_id` links them and the conversation's `leaf_id` marks the
branch on screen, which is how edits, retries and rewind keep every version. A message's `blocks` are
`text`, `thinking`, `tool`, `attachment`, `notice`, `error` and a few more; [API.md](API.md) lists them.

## Tools and permissions

A tool is a `Tool` entry in `TOOLS` (`agent/tools.py`): a name, a category (`read`, `edit`, `exec`, `net` or
`meta`), a description and its parameters as JSON Schema. `Agent._tool` runs a call:

1. `PreToolUse` hooks may refuse it.
2. `permissions.decide` gives allow, ask, deny or judge. It checks the account's rules first (deny, then ask,
   then allow), then the category and the mode: reads inside the allowed folders run; edits depend on the
   mode and on where the file is; commands go through `shellcheck.classify`, which sorts them into safe,
   dangerous or unknown. An allow rule never widens the folders an account may touch.
3. `judge` (Auto mode) asks a model for a verdict; a model too small to judge, or an unreadable verdict, means
   ask. [AUTO_MODE.md](AUTO_MODE.md) has the details.
4. `ask` creates a pending approval: the turn waits in `_wait_for_person` until someone who may answer does
   (`POST /api/pending/{id}`).
5. Before the first edit or command of a turn in a working folder, `Checkpoints.snapshot` records the folder,
   so that rewind can put it back.
6. `Agent._execute` does the work. File tools run in the server with the path checks above; commands and
   hooks run in the sandbox.

To add a tool:

1. Add a `Tool(...)` to `TOOLS`, with the category that fits: permissions follow from it.
2. Add its branch to `Agent._execute`, returning `(text, extra)`. Blocking work goes through
   `asyncio.to_thread`; failures raise `ToolError`, which the model sees as `Error: …`.
3. If it is not always offered, gate it in `tools.available()`.
4. Show it: `TOOL_ICON`, `toolTitle` and `toolDetails` in `web/js/views/blocks.js`; `DOING` in
   `web/js/thread.js` and `tui/thread.py`.
5. Test it with scripted `tool_calls` in `tests/test_agent.py`.

## The sandbox

Every command the model runs goes through `sandbox/__main__.py`, a helper that confines itself and then
executes the command. If it cannot confine itself, it refuses to run (exit status 126).

- **Linux**: Landlock makes the working folder and a few granted folders writable, system folders readable,
  and everything else invisible; with a recent kernel it also limits TCP to web ports, or to nothing when the
  network is off. A seccomp filter refuses tracing, reading other processes' memory and a few more calls.
- **macOS**: a Seatbelt profile (`sandbox-exec`) written for each command denies reading and writing in user
  folders and writing everywhere, then allows the granted folders; the network is off unless allowed.

`sandbox/check.py` runs commands in the sandbox and checks each promise (`baabaa doctor --sandbox`); the
tests on GitHub run it on both systems. Not in the sandbox: the file tools (in the server, with path checks),
the web tools (in the server, which refuses private and local addresses), connectors that are programs on
this computer, and model servers.

## Models and the GPU

`models.ModelRegistry` mirrors Ollama's models into the `models` table. `ModelJobs._fit` is the fit test:
it loads a model at growing context sizes, each of which must be entirely in GPU memory, then measures speed
and tool use. Only a model that passed and that an owner approved gets a context size, and `registry.num_ctx`
refuses any other, so an unapproved model cannot be used by any path.

The rules the gateway enforces at run time (loaded entirely on the GPU, one model program at a time, no
model work on the CPU) and the model programs baabaa starts itself (`llamacpp.py`, `sdcpp.py`) are described
in [RUNTIMES.md](RUNTIMES.md). `gpu.py` reads an NVIDIA card through NVML; on Apple silicon it knows the
memory budget but has no per-second readings.

## Data on disk

Everything is under the data folder (`paths.py`): `$BAABAA_HOME`, or `~/.local/share/baabaa`.

- `baabaa.db` (`maindb.py`): accounts, sessions, machine settings, models, jobs, folder grants, trusted
  folders, API keys, shares.
- `accounts/<id>/account.db` (`store.py`): conversations, messages, full-text search, attachments, artifacts
  and their versions, rules, checkpoints, projects and their documents, memories, schedules.
- `accounts/<id>/files/`: uploads, each chat's own folder, checkpoint contents, customization.
- `stats.db` (`stats.py`): [STATISTICS.md](STATISTICS.md).

Schemas change by adding a migration to the module's `MIGRATIONS` list (`db.migrate`); existing entries are
never edited.

## Accounts and security

- Two roles: owner and member. Passwords are optional, hashed with scrypt. Sessions are random tokens stored
  as hashes, in an `HttpOnly`, `SameSite=Strict` cookie.
- Owners choose models, manage accounts, and may use any folder. Members use the folders granted to them.
- Adding anything that runs a program outside the sandbox (a connector that is a program, a model server)
  needs an owner with a password who types it again, or the terminal on the machine itself
  (`Web._confirm_host_access`).
- Model output is untrusted everywhere. The Markdown renderer never passes HTML through. Artifacts are shown
  in a sandboxed frame with a policy that allows no network. Uploaded files are served inline only when they
  are plain images.
- Every response carries a Content-Security-Policy that allows scripts from baabaa itself only.

## The web app

No framework and no build step: `index.html` loads `js/app.js` as a module.

- `app.js` holds the shared state `S`, boots the app, and routes by the URL's hash to a view in `js/views/`,
  loaded when first needed. `changed(what)` tells listeners registered with `onState` that part of `S` changed.
- `api.js` makes requests and holds the event stream: `on('msg.delta', fn)` and so on.
- `dom.js` has `h(tag, attrs, ...children)` for building elements, the icons and the formatters. Text is set
  as text; `html:` is used only for the renderers' output and for icons.
- `markdown.js`, `highlight.js`, `mermaid-lite.js` and `thread.js` are classic scripts that set a global
  (`BaabaaMarkdown` and so on). That lets the same files be pasted into exported pages and artifact frames,
  and be tested under plain Node (`tests/js/`).
- `css/app.css` is the one stylesheet: colours and sizes as custom properties on `:root`, dark values under
  `:root[data-theme="dark"]`.

To add a view: a module in `views/` that exports `renderSomething(main)`, a branch in `renderMain`
(`app.js`), an entry in the sidebar (`views/sidebar.js`). To add a live event: publish it with
`app.events.publish(account_id, "name", data)`, add the name to `TYPES` in `api.js` (the browser hears only
the names it listens for), handle it with `on('name', fn)`, and in `TUI.handle` if the terminal should react.

## The terminal app

`baabaa` with no command runs `tui/app.py`: a curses program that talks to the running server over the Unix
socket, with the same HTTP API and event stream as the browser (`tui/client.py`). `TUI.handle` applies
events, `build_lines` turns the conversation into lines, `draw` paints them. `tui/thread.py` is the text
form of `thread.js`.

## Extensions

`extend.py` reads customization from two places, the second overriding the first: the account's own folder,
and `.baabaa/` in a trusted working folder. Skills, slash commands, helper agents and reply styles are
Markdown files with a small header; hooks are commands that run, sandboxed, before or after a tool, when a
message is sent, and when a reply ends. `mcp.py` is the client for connectors (MCP servers, over HTTP or as a
program on this computer). `scheduler.py` runs scheduled tasks by sending a message as the account would.

## Updates, releases and the website

An installed baabaa keeps each version in its own folder and switches a link (`update.py`); the release list
is signed and checked with `signing.py`; `restarter.py` waits for the replies being written and restarts in
place. [RELEASING.md](RELEASING.md) describes making a release with `tools/release.py`.

`tools/website.py` builds the website into `dist/site`: a landing page, these documents, and a preview of the
interface that needs no server. The preview is the files in `baabaa/web/`, unchanged, plus `website/preview.js`,
which stands in for the server in the page. Its data is made at build time by running the real server
against the tests' stand-in for Ollama with the conversations in `website/script.py`, so it cannot drift from
the real interface. `python3 tools/website.py serve` builds it and serves it on this computer to look at.

## Tests

`python3 -m unittest discover -s tests`, with `unittest` only.

| File | What it covers |
|---|---|
| `test_core.py` | Rules and modes, the shell check, the judge's parsing, accounts, storage, file tools, checkpoints, context building, document reading, model search, the terminal thread. |
| `test_agent.py` | Whole turns through a real `App` against `fixtures/fake_ollama.py`: tools, projects, memory, research, schedules, images, dictation, recovery from a model's mistakes. |
| `test_server.py` | The real server on a spare port: host and CSRF checks, setup, sharing, worktrees, the sandbox. |
| `test_runtimes.py` | Model programs other than Ollama, with `fixtures/fake_llama_server.py`. |
| `test_platform.py` | Network modes, the macOS parts (parsers, the Seatbelt profile, launchd), the sandbox self-test. |
| `test_update.py` | Signatures, the release list, installing, switching, restarting into a new version and falling back. |
| `test_daemon.py` | Start, status, stop. |
| `test_site.py` | The website builds, and nothing of the machine that built it is in it. |
| `tests/js/*.test.mjs` | The renderers and the thread, under plain Node. |

`FakeOllama().script([...])` gives the stand-in the replies of the next model calls: `content`, `thinking`,
`tool_calls` or `error`. Tests never need a GPU or the network. GitHub runs all of it on Linux and on macOS
with Apple silicon for every push (`.github/workflows/test.yml`).

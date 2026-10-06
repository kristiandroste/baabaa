# baabaa

A self-hosted assistant and coding agent for your own computer or your local network, running local models
through [Ollama](https://ollama.com) on your own GPU: Linux with an NVIDIA GPU, or a Mac with Apple silicon
(new: see Requirements). One Python program serves the same conversations to a browser (on this computer, or
on any device on your network if you allow it) and to a terminal.

- **Chat and code in one place.** A conversation is a chat; give it a working folder and it can read,
  edit and run things there. Browser and terminal are two views of the same history.
- **Four modes** for actions: Manual (ask before every edit and command), Accept edits, Plan (read only,
  then a plan to approve) and Auto. Auto runs actions that pass three checks: your rules, a shell-command
  check, and a judgement by the local model. Anything risky waits for approval. In a working folder, reading
  a web page from a new site asks too ("Always allow" adds the site; in Auto the judge decides).
- **Sandboxed tools.** Every command runs under Landlock and seccomp (Seatbelt on macOS): it can change only
  the working folder, sees only system directories and folders granted to the account, and has no network
  unless allowed (then only web ports). `baabaa doctor --sandbox` runs commands in it and checks each promise.
- **Accounts**, password optional, each with its own conversations and files.
- **GPU-only models.** A model is offered only after a fit test shows it loads 100% into GPU memory, and
  the owner approves it. The test also checks how well the model uses tools, and the model picker warns
  about one that failed. Every request checks residency before any prompt is processed, and a CPU guard
  stops any model program that starts computing on the CPU (the fans-and-heat case the rules exist to
  prevent). Model servers keep their memory at normal-program scale with bounded caches; `baabaa doctor`
  shows the two settings that cap Ollama's.
- **Find models:** search the Ollama library in your own words ("best model for coding") or by category
  (coding, vision, reasoning, agents and tools, small and fast, embeddings), or ask for newer versions of
  one model you have or all of them. Only models that fit the GPU are suggested, chosen by your default
  model; suggestions you dismiss stay away. Installs run in the background and are tested automatically.
- **Models Ollama cannot run** (for example 1-bit and ternary models that need their own llama.cpp build)
  run through the same queue and checks: baabaa starts the model's server itself, one at a time, and
  refuses it unless every layer, the KV cache and the encoder are on the GPU and no copy sits in system
  memory. Images and audio for Ollama models go the same way, because Ollama runs their encoder on the
  CPU. See [docs/RUNTIMES.md](docs/RUNTIMES.md).
- **Projects:** conversations that share instructions and documents. Documents go into the prompt whole
  while they fit; beyond that each message retrieves the matching passages (keyword search, plus semantic
  search computed on the GPU by a small CUDA kernel).
- **Memory** across conversations (yours to view and edit), and search of earlier chats.
- **Code execution and files in chats:** each chat has a private, sandboxed folder where the model can run
  Python and make Word, Excel, PowerPoint and PDF files (written by baabaa itself) for you to download.
- **Research:** a question answered from several web sources, read and summarized one by one, with
  numbered citations.
- **Images:** make and edit pictures with open image models served by stable-diffusion.cpp (for example
  Z-Image-Turbo, fast; Qwen-Image-2.1, the most capable open model, run staged from disk), from the Image
  switch or by asking in any conversation; all of them on the Images page.
- **Dictation** transcribed on this computer's GPU by a model that hears audio (the audio is not kept).
- **Scheduled tasks**, **sharing** conversations between the accounts here, **git worktrees** for
  coding conversations, built-in `/review`, `/security-review` and `/init`, and slide decks and
  documents that download as PowerPoint, Word or PDF.
- **Usage statistics** for analysis: every model request, tool call, approval and GPU sample, as metadata
  (never content), exportable as CSV, JSON Lines or SQLite.
- **A thread, not a spinner:** while a reply is written, a strand of wool curls as the model's words arrive,
  hangs slack while it waits, and winds each thought into a ball of yarn, bigger for a longer thought. The
  terminal draws the same thread as a line of text.
- Streaming with instant stop, queued messages, branching (edit and retry), compaction, rewind (files
  and conversation), artifacts (web pages, documents, diagrams, code; a complete page written in a reply
  becomes one too), uploads (text, code, PDF, Office,
  images), exports (Markdown, HTML, JSON, PNG), search, incognito chats, dark mode, read aloud with the
  device's own voices, notifications, installable as an app (PWA), phone layout.

No dependencies: the Python standard library and hand-written HTML, CSS and JavaScript. No build step.

## Requirements

- Linux with Python 3.10 or newer (Landlock needs Linux 5.13+; 6.7+ also restricts network ports). Ubuntu
  22.04 or newer, Debian 12 and Fedora come with both. An NVIDIA GPU for models; readings for the statistics
  use NVML, part of the driver.
- Or a Mac with Apple silicon, with Python 3.10 or newer from [python.org](https://www.python.org/downloads/macos/)
  or Homebrew (`brew install python`; the `python3` that comes with macOS is too old). Models run on the GPU
  through Metal, which shares the computer's memory: about two thirds of it can hold models (three quarters
  above 36 GB). Tested on macOS 15 (2026-10-05): the installer, the tests and the sandbox's checks pass. Models
  on a Mac's GPU are not tested yet, and on another macOS version run `baabaa doctor --sandbox` first. Not on
  the Mac yet:
  models that run outside Ollama, GPU search of project documents (keyword search instead), and per-second GPU
  statistics. On an Intel Mac, Ollama computes on the CPU, so baabaa installs but runs no models.
- [Ollama](https://ollama.com/download) running on the same machine (default `http://127.0.0.1:11434`).
- `openssl` for HTTPS when other devices connect (Linux and macOS both have it).

## Install

```sh
curl -fsSL https://github.com/kristiandroste/baabaa/releases/latest/download/install.sh | sh
```

No sudo and no pip. The installer needs Python 3.10 or newer, downloads the release and checks its SHA-256,
puts baabaa in `~/.local/lib/baabaa` and the `baabaa` command in `~/.local/bin`, says whether it found Ollama
and a GPU, and asks three things: whether other devices on your network may connect (no, unless you say so),
whether to start baabaa now, and whether to start it with the computer. `sh -s -- latest` installs from the latest
channel and `sh -s -- 1.2.3` a given version. The self-contained installer `baabaa-VERSION-install.sh`
carries the program inside and installs the same way without a download: `sh baabaa-0.9.0-install.sh`.

## Start

```sh
baabaa doctor        # checks Python, SQLite, Landlock, GPU, Ollama and the installation
baabaa start         # the server, in the background, on port 8443
baabaa status        # is it running, where to open it, what the GPU is doing, updates
baabaa stop          # stops it, with its model servers; `baabaa restart` starts it again, same options
baabaa autostart on  # start it with the computer (systemd on Linux, launchd on macOS); `off` undoes it
baabaa network       # who can connect: this computer only, or other devices too
```

`baabaa start` keeps running after the terminal that started it closes; its output goes to
`logs/server.log` in the data folder. `baabaa serve` runs the server in the terminal instead. One server
runs per data folder.

The first start prints a setup link for the owner account (`baabaa status` shows it again until the
account exists; a browser on the same computer needs no link). The home screen then offers to find a first
model: it suggests models from the Ollama library that fit your GPU. Installing one downloads it and runs the
fit test; you then approve it in **Settings → Models**. Approved models appear in the model picker.

New to all this? [docs/TUTORIAL.md](docs/TUTORIAL.md) goes through it step by step.

When other devices may connect, install the local certificate authority once on each (`/ca.crt`, or run with
`--ca-port 8080` to serve it over plain HTTP) so the browser trusts baabaa and allows the microphone.

Terminal: `baabaa` in a folder starts a conversation that works in that folder (`baabaa chat --no-folder`
for a plain chat; `/help` for commands; `/research QUESTION` for a research report).

When another program needs the GPU: `baabaa gpu pause --reason "…"` (or the switch in **Settings →
Models**) unloads baabaa's models and holds new GPU work in the queue until `baabaa gpu resume`.
`baabaa gpu status` shows what holds GPU memory.

Accounts from the command line: `baabaa account add NAME [--password]`, `account grant NAME /path [--ro]`,
`account list`.

## Update

An installed baabaa checks for a new release once a day (a download of the release list from GitHub, which
carries nothing about you) and downloads it ahead of time. Every account then sees a notice (an owner can
leave installing and restarting to owners, in **Settings → About**): **Install** when a release is available,
**Restart** when it is downloaded or installed and only a restart is left. A restart waits for the replies being written (or goes
at once); open windows reconnect and load the new version by themselves, keeping a message being typed.
The terminal UI says the same and has `/update` and `/restart`.

```sh
baabaa update            # install the newest release; the server restarts once it is idle (--now: at once)
baabaa update --check    # only say whether there is one
baabaa install 1.2.3     # any version, also an older one
```

The release list is signed (Ed25519), and baabaa checks the signature and each download's SHA-256 before
using anything. Versions sit side by side, the switch is one step, the previous two versions stay, and
**Settings → About → Use previous version** goes back; if a new version fails to start, baabaa goes back by
itself. Before each switch, baabaa copies its databases to `backups/` in the data folder (the last two are
kept). Channels: **stable** (the default; a release that has been out for a week) or **latest**.
`BAABAA_DISABLE_AUTOUPDATER=1` stops the daily check; `BAABAA_DISABLE_UPDATES=1` stops updates altogether.

## Remove

```sh
baabaa uninstall          # the program; your data stays
baabaa uninstall --purge  # also baabaa's data: accounts, conversations, files, settings (it asks first)
```

Neither touches Ollama: the program, its service and settings, and all its models (also those installed
through baabaa) stay, as do model programs and files baabaa used where they are; `--purge` lists them.

## Where things live

Everything is under `$BAABAA_HOME` (default `~/.local/share/baabaa`): `baabaa.db` (accounts, models,
settings), `stats.db` (usage statistics), `accounts/<id>/` (each account's conversations, uploads,
artifacts, projects, memory, chat folders and file checkpoints), `logs/` (the background server's output),
`runtime/` (logs of model servers baabaa starts), `tls/` (the local certificate authority) and `backups/`
(database copies from before updates). The program is in `~/.local/lib/baabaa` (`versions/`, one folder per
version, and `current`), the command in `~/.local/bin/baabaa`.

## Network

Who can connect is the owner's choice: the installer asks, `baabaa network local|lan` changes it, and so does
**Settings → About → Network** (a restart applies it). A new data folder starts with this computer only.

- **This computer only** (`local`): baabaa listens on 127.0.0.1 over plain HTTP, at `http://localhost:8443`.
  Browsers treat localhost as secure, so the microphone and app install work without a certificate.
- **Other devices too** (`lan`): baabaa also listens on the address of the interface with the default route,
  over HTTPS with its own certificate authority (install `/ca.crt` once on each device), and accepts clients
  from loopback and that subnet. Every account without a password can then be opened by anyone on the network.

Data folders from before this setting existed keep `lan`. In both modes IPv6 is not served, requests must use one of
the host's own names or addresses (against DNS rebinding), and state-changing requests need a CSRF token and a
same-origin `Origin`. Options: `--bind` (choose the addresses yourself), `--allow CIDR`, `--name HOST`,
`--port`, `--http`.

## Development

From a checkout, `bin/baabaa` runs that folder's copy (link it once: `ln -s "$PWD/bin/baabaa" ~/.local/bin/baabaa`).
A checkout does not update itself; after a `git pull`, the browser offers a restart.

```sh
python3 -m unittest discover -s tests                # Python tests (no GPU needed)
for t in tests/js/*.test.mjs; do node "$t"; done     # renderer tests (Node 18+)
```

The Python tests use stand-ins for Ollama and llama.cpp (`tests/fixtures/`), so they need no GPU.

How baabaa is built: [docs/DEVELOPING.md](docs/DEVELOPING.md). A first walk through it, for anyone:
[docs/TUTORIAL.md](docs/TUTORIAL.md).
Statistics schema: [docs/STATISTICS.md](docs/STATISTICS.md). HTTP API: [docs/API.md](docs/API.md).
Auto mode: [docs/AUTO_MODE.md](docs/AUTO_MODE.md). Other model programs: [docs/RUNTIMES.md](docs/RUNTIMES.md).
Making a release, and the website: [docs/RELEASING.md](docs/RELEASING.md). What baabaa protects, and its limits:
[SECURITY.md](SECURITY.md).

## License

Copyright © 2026 kristiandroste. baabaa is free software under the GNU Affero General Public License, version 3
([LICENSE](LICENSE)): you may use, study, change and share it, and if you run a changed version for other
people, including over a network, you must offer them its source under the same license. Other license terms
are available from the author. To contribute, see [CONTRIBUTING.md](CONTRIBUTING.md); pull requests ask for a
contributor agreement ([CLA.md](CLA.md)).

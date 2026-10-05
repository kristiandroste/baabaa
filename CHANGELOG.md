# Changelog

The notes for each release. `baabaa update` and the browser's update notice show them before an update,
and every account sees the newest section once afterwards.

## 0.9.2 - 2026-10-05

- Macs: the installer stopped at its download step on macOS; fixed. On macOS 15 with Apple silicon the installer,
  updates and restarts, the tests and the tool sandbox's checks now pass. Models on a Mac's GPU are not tested yet.
- The CPU guard finishes stopping a model program before the reply reports it.
- baabaa is licensed under the GNU AGPL, version 3. Contributions need the contributor agreement (CLA.md).

## 0.9.1 - 2026-10-02

- Choose who can connect: this computer only (plain HTTP on localhost, no certificate needed) or other devices
  on your network too. The installer asks; change it with `baabaa network` or in Settings → About → Network.
- Macs with Apple silicon: the installer, models through Ollama on the GPU, the tool sandbox (Seatbelt),
  starting with the computer (launchd). New and not yet tried on a Mac: run `baabaa doctor --sandbox` first.
- `baabaa doctor --sandbox` runs commands in the tool sandbox and checks each of its promises on your machine.
- Safer auto and plan modes: a second command on a new line is checked like the first.

## 0.9.0 - 2026-10-02

A preview for testers.

- Install with one command, without sudo or pip, and update from the terminal (`baabaa update`) or from the
  browser: a notice says whether a release is available (Install) or only a restart is left (Restart).
- Releases are signed and every download is checked before it runs; versions sit side by side, a copy of the
  databases is taken before each switch, and baabaa goes back by itself if a new version does not start.
- Restart baabaa from any browser or the terminal (`/restart`). It waits for the replies being written, and an
  owner can allow it for every account or only for owners.
- Before the first model: the home screen suggests models from the Ollama library that fit your GPU.
- Chats, projects, memory, artifacts, images, research, dictation and scheduled tasks in the browser, and the
  same conversations in the terminal.
- Coding in your folders with four modes (manual, accept edits, plan, and auto with a local judge), sandboxed
  commands, rewind and git worktrees.
- Several accounts, with or without passwords, each with its own data, and usage statistics that never hold
  content.

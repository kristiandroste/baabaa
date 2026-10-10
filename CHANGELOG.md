# Changelog

The notes for each release. `baabaa update` and the browser's update notice show them before an update,
and every account sees the newest section once afterwards.

## 0.9.4 - 2026-10-10

- On the network, baabaa has names: phones and computers there open it as `baabaa.local:8443` or `ai.local:8443`,
  with no address to remember (Linux for now; on a Mac, the Mac's own name). `--name` on `baabaa start` adds one.
- An address typed without `https://` opens baabaa instead of failing: plain HTTP on its port gets a redirect to
  HTTPS. The one thing served without HTTPS is the certificate itself, so a phone can fetch it before it trusts
  baabaa (an iPhone's installer fetches it on its own, and over HTTPS it failed silently).
- Installing baabaa's certificate on a phone or computer is explained step by step, for each kind of device, in
  Settings → About and in the tutorial. The certificate is sent so that Safari and Firefox offer to install it
  directly.
- Fixed on phones: fields with small text made iPhones zoom the page in, and the zoom stayed; with the phone on
  its side, the bottom of the sidebar (settings, the account) was out of reach; on short screens the greeting was
  pushed above the top edge; the send button left its corner when the model's name was long, and a long name
  could widen the whole page.

## 0.9.3 - 2026-10-05

- A thread in place of the trotting sheep. While baabaa works on a reply, a strand of wool curls as the model's
  words arrive, hangs slack while the reply waits for the GPU (and says how many are ahead of it), carries a bead
  while a tool runs, and calms to a ripple while the answer is written. When no words come for a moment it sags,
  so a stuck model looks stuck.
- Each finished thought folds into a ball of yarn with the time it took; a longer thought leaves a bigger ball.
  Click it to read the thought.
- The terminal shows the same thread as one line of text.
- While working in a folder, baabaa asks before reading a page from a site it has not read from before, as
  Claude Code does; "Always allow" adds a rule for the site. Auto mode's judge decides instead of asking. Chats
  without a folder read pages as before. (A misled model could otherwise carry what it read in the folder to
  someone else in a page's address.)
- A new data folder starts with this computer only, however baabaa was started; to let other devices in, say so
  (`baabaa network lan`, or Settings → About → Network). Data folders that already had accounts keep what they had.
- Fixed: a diagram or page at the very end of a reply could stay undrawn until the conversation was reopened;
  adding a connector that is a program on this computer failed from the browser (the form now asks for the
  password the server requires); pausing the GPU left a running image model in place.
- Safer: refused requests carry the same security headers as every other answer; the web tools connect to
  the very addresses they checked, so a name server cannot redirect a fetch to this computer; passwords are
  hashed at a higher cost (existing ones are renewed at the next sign-in). SECURITY.md says what baabaa
  protects and where its limits are.
- New documents: a tutorial from an empty machine to the first reply (docs/TUTORIAL.md) and a developer guide
  (docs/DEVELOPING.md).

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

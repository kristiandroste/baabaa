# Security

## Reporting a problem

Please report security problems privately: on the repository's **Security** tab, choose **Report a
vulnerability**. Do not open a public issue for them. Say what you did, what happened, and which version
(`baabaa --version`). Fixes go into the newest release; older releases are not patched.

## What baabaa is built for

baabaa runs on your own computer, for you or for the people on a home or office network you trust. It is not
built to face the internet: do not forward a port to it or put it on a public address.

What it does to keep that setting safe:

- **Who can reach it.** A new installation accepts this computer only. When the owner opens it to the
  network, it accepts devices on the same subnet only, over HTTPS with its own certificate authority, and
  only under this computer's own names and addresses. Requests that change anything need a session, a
  same-origin `Origin` and a CSRF token. It announces its names (`baabaa.local`, `ai.local`) by multicast DNS,
  answering only for its own names and only to that subnet; plain HTTP on its port gets nothing but a redirect to HTTPS.
- **Accounts.** Passwords are optional and stored with scrypt; repeated wrong passwords are refused for a while.
  Members use only the folders an owner grants them.
- **What the model may do.** Edits and commands go through the conversation's mode, the account's rules, a
  check of the command line and, in Auto mode, a judgement by a model. Every command runs in a sandbox that
  can change only the working folder and has no network unless it is allowed, and then only web ports.
  `baabaa doctor --sandbox` checks those promises on your machine.
- **What the model writes.** Model output is never trusted: Markdown is rendered without passing HTML
  through, and pages the model writes are shown in a sandboxed frame that has no network.
- **Programs on the computer.** Adding a connector or model server that runs outside the sandbox needs an
  owner with a password who types it again, or the terminal on the machine itself.
- **Models stay on the GPU.** A model that does not fit entirely in GPU memory is refused.
- **Updates.** The list of releases is signed (Ed25519), every download is checked against it, and a version
  that does not start is rolled back.
- **Nothing phones home.** No telemetry. Usage statistics are metadata, never content, and stay in a
  database on your machine. baabaa contacts the internet only for what you ask: the daily check of the
  release list, the Ollama library when you look for models, and the web tools when a conversation uses them.

## Known limits

- **An account without a password can be opened by anyone who can reach baabaa.** On this computer only,
  that is whoever uses the computer. On the network, it is every device there. baabaa says so when you open
  it to the network.
- **A model can be misled by what it reads.** A web page, a file or a tool's output can contain instructions
  aimed at the model. Modes and the sandbox limit what a misled model can do, and Auto mode's judge is itself
  a model and can be wrong. For folders that matter, use Manual or Accept edits and read what you approve.
- **A name or address typed without `https://` starts in plain HTTP.** That first request is not encrypted,
  and names on the network are not authenticated, so another device there could answer instead of baabaa.
  With baabaa's certificate installed, the browser warns when the answer does not come from baabaa; a
  bookmark with `https://` skips the plain request.
- **Chats without a folder read web pages without asking.** A conversation that works in a folder asks before
  the first page from a site (Auto mode's judge decides instead), because a misled model could put something
  it has read there into the address of a page it fetches. A chat without a folder reads pages freely, like
  the chat apps do, so the same could happen with what is in the conversation. Web searches never ask. A deny
  or ask rule for `WebFetch` in Settings → Permissions restricts this everywhere.
- **On Linux, a sandboxed command can read `/proc`**, which includes the command lines and environment
  variables of other programs running as the same user. baabaa's own server runs with a cleaned environment
  and keeps its keys in files the sandbox cannot read, but it cannot hide your other programs. Running baabaa
  as a user of its own avoids this.
- **On macOS the sandbox is looser about reading**: it denies user folders and allows system files, and there
  is no system-call filter. Models on a Mac's GPU are not tested yet.
- **File tools are checked, not sandboxed.** Reading and editing files happens in the server, with checks on
  the path. Commands are what run in the sandbox.
- **Data is stored as plain files.** Conversations, uploads and statistics are in the data folder, readable
  by the user who runs baabaa and protected only by file permissions. Back the folder up, and encrypt the
  disk if the machine can be lost.
- **The certificate authority's key is in the data folder.** A device that trusts baabaa's certificate
  authority trusts whoever holds that key.

# Releasing baabaa

How a release is made, signed and published, and how installed copies use it. For the maintainer; users
need only the README.

## What a release is

| File | What it is |
|---|---|
| `baabaa-VERSION.tar.gz` | The program: `baabaa/`, `bin/baabaa`, `install.sh`, `README.md`, `CHANGELOG.md`, `LICENSE`, `docs/` (`update.RELEASE_FILES`), under one folder `baabaa-VERSION/`. Built reproducibly: sorted entries, one timestamp (the release date), no owner names, normalised permissions, gzip without time or name. The same files give the same bytes. |
| `releases.json` | The release list: every release with its date, download address, SHA-256, size, minimum Python, notes, and the `stable` and `pulled` flags. |
| `releases.json.sig` | `<key id> <Ed25519 signature, hex>` over the exact bytes of `releases.json`. |
| `install.sh` | The installer (POSIX sh). |
| `baabaa-VERSION-install.sh` | The installer with the release inside, for handing to someone directly. Not uploaded. |

Every GitHub release carries `releases.json` and its signature, so
`https://github.com/kristiandroste/baabaa/releases/latest/download/releases.json` is always the newest list.

## Trust

- Installed copies (`baabaa update`, the daily check, the browser's Install) accept a release list only when
  its signature verifies against a key in `baabaa/signing.py` (`RELEASE_KEYS`), and a download only when its
  size and SHA-256 match the list. The check is RFC 8032's verification in plain Python (the standard library
  has none), tested against the RFC's vectors (`tests/fixtures/ed25519_vectors.json`).
- The first install (`install.sh`) checks the download's SHA-256 against the list it fetched over HTTPS from
  the same place, as other one-line installers do; the signature protects every update after that.
- A new version must pass `baabaa selfcheck` (every module imports, the web files exist) before baabaa
  switches to it or restarts into it. If it then fails to start, the new process switches back to the
  version it came from and starts that, and the update notice shows why.

## The key

The signing key is an Ed25519 key in `~/.config/baabaa-release/signing-key.pem` (mode 600) on the
maintainer's machine; it never goes into the repository. Keep a copy somewhere safe: installed copies trust
only the keys their version knows. To change keys, add the new public key to `RELEASE_KEYS` in a release
signed with the old one, and sign with the new key only after that release has gone out.

```sh
openssl genpkey -algorithm ed25519 -out ~/.config/baabaa-release/signing-key.pem    # once
openssl pkey -in ~/.config/baabaa-release/signing-key.pem -pubout -outform DER | tail -c 32 | od -An -tx1 | tr -d ' \n'
```

## Private words

`~/.config/baabaa-release/private-words.txt` lists what must never be published: names, addresses, this
machine's user and host names, private folders, other projects. One regular expression per line, matched
regardless of case. `tools/release.py check` searches every file git would publish and the whole git history
(authors, committers, messages and every change in every commit) for them, and `build` and `publish` refuse
while anything matches. Commits use the GitHub no-reply address (`git config user.email`), never a personal one.

## Making a release

1. Set `__version__` in `baabaa/__init__.py` and add a dated section to `CHANGELOG.md`
   (`## 1.2.3 - 2026-10-02`, then `- ` items: these are the notes people see).
2. `python3 tools/release.py build` runs the private-words check and the tests, builds `dist/VERSION/`, adds
   the release to `dist/releases.json` and signs the list. `--stable` puts the release on the stable channel
   at once (a first release, an urgent fix); otherwise stable gets it after 7 days. `--local` writes file
   names instead of GitHub addresses, for a test feed in a folder (`BAABAA_UPDATE_URL=file:///path/to/dist/VERSION`).
3. `python3 tools/release.py publish VERSION --go` creates the GitHub release with `gh` and uploads the
   archive, `install.sh` and the signed list (without `--go` it only prints the command).

To withdraw a release: `tools/release.py pull VERSION` marks it `pulled` and signs the list again, then
`tools/release.py publish NEWEST --list-only --go` uploads the list to the newest release. Copies that already
installed it keep it until the next release; nothing new installs it.

## Channels

`latest` gets every release that is not pulled. `stable` gets a release once it has been out for 7 days, or
at once when it was published with `--stable`. The installer falls back to the newest release when stable has
none yet. An install's channel is in `~/.local/lib/baabaa/update.json` and in **Settings → About**.

## The website

`python3 tools/website.py build` makes the site in `dist/site`: the landing page, the documents (the Markdown
files of this repository, shown by a small reader) and the preview. `python3 tools/website.py serve` builds it
and serves it at `http://127.0.0.1:8900/` on this computer only, to look at.

The preview is the interface in `baabaa/web/`, unchanged, with `website/preview.js` standing in for the
server. Its data is recorded at build time: the real server runs in the build's own process, on 127.0.0.1,
against the tests' stand-in for Ollama, and plays the conversations written in `website/script.py`; the
commands in them really run, in the sandbox, in a sample project. Nothing in the preview runs a model, and
the page says so.

The build refuses to finish if the result names the machine it ran on: its home folder, host name, user
name, a network address, the build's own folder, or a private word. The GPU in the preview is a stand-in,
and folders are renamed to `/home/guest/…`.

Publishing is the workflow **website** (`.github/workflows/website.yml`), started by hand from the
repository's Actions tab once GitHub Pages is turned on (Settings → Pages → Source: GitHub Actions; a custom
domain is set on the same page). It builds on GitHub's machine and publishes the result. Nothing publishes
the site by itself.

# Contributing to baabaa

Bug reports and ideas are welcome as GitHub issues. For a larger change, open an issue first so the approach
can be agreed before you write it.

## The rules of the code

- The Python standard library and hand-written HTML, CSS and JavaScript only: no third-party packages, no
  vendored libraries, no build step.
- Models run entirely on the GPU. A change must never let model work run on the CPU (`docs/RUNTIMES.md`).
- Statistics record metadata, never content (`docs/STATISTICS.md`).
- The tests pass: `python3 -m unittest discover -s tests` and `for t in tests/js/*.test.mjs; do node "$t"; done`.
  None of them needs a GPU.

## The contributor agreement

Your first pull request asks you to sign the contributor license agreement ([CLA.md](CLA.md)) with a comment.
You keep the copyright in your work; the agreement lets baabaa also be licensed under terms other than the
AGPL-3.0.

## Security problems

Please report them privately, through "Report a vulnerability" on the repository's Security tab, not in a
public issue. [SECURITY.md](SECURITY.md) says what baabaa protects and where its limits are.

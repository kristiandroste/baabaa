"""baabaa: a self-hosted assistant and coding agent on local Ollama models, for one computer or its local network."""

__version__ = "0.9.4"
NAME = "baabaa"

# How baabaa starts a copy of itself (with a clean environment, for a restart, to check a new version):
# `python3 -c BOOT ROOT ARGS...` runs the baabaa in ROOT, never a `baabaa` folder in the current directory.
BOOT = ("import sys; r = sys.argv.pop(1); sys.path[:] = [r] + [p for p in sys.path if p not in ('', '.')]; "
        "from baabaa.__main__ import main; main()")

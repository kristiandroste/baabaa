"""`baabaa doctor --sandbox`: run commands inside the tool sandbox and check each of its promises on this
machine, with throwaway folders in the data folder. Every check prints ok or NO; any NO means the sandbox
does not hold here and must not be trusted."""

import os
import shutil
import socket
import subprocess
import tempfile
from pathlib import Path

from . import helper_argv, sandbox_env

SECRET = "sheep-secret-7f3a"


def _run(command: str, work: Path, rw: list[str], ro: list[str], net: bool, home: str, tmp: str, ports=(80, 443)):
    argv = helper_argv(["/bin/bash", "-c", command], str(work), rw, ro, net, ports)
    env = sandbox_env(home, tmp, roots=[str(work)] + rw + ro)
    try:
        res = subprocess.run(argv, cwd=str(work), env=env, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    return res.returncode, (res.stdout + res.stderr).strip()


def run_checks(base: Path) -> list[tuple[str, bool, str]]:
    root = Path(tempfile.mkdtemp(prefix="sandbox-check-", dir=str(base)))
    out: list[tuple[str, bool, str]] = []
    listener = None
    try:
        work, granted, outside, home, tmp = (root / n for n in ("work", "granted", "outside", "home", "tmp"))
        for d in (work, granted, outside, home, tmp):
            d.mkdir()
        (granted / "ro.txt").write_text("readable\n")
        (outside / "secret.txt").write_text(SECRET + "\n")
        rw = [str(work), str(home), str(tmp)]
        ro = [str(granted)]

        def check(name, command, want_ok, also=None, net=False, ports=(80, 443)):
            code, text = _run(command, work, rw, ro, net, str(home), str(tmp), ports)
            if code == 126 and "refusing to run unconfined" in text:
                out.append((name, False, "the sandbox could not start: " + text.splitlines()[-1]))
                return
            good = (code == 0) == want_ok and (also is None or also(text))
            out.append((name, good, "" if good else f"exit {code}: {text[-200:]}"))

        check("runs a command", "echo ok", True, lambda t: "ok" in t)
        check("writes in its working folder", "echo x > a.txt && cat a.txt", True)
        check("cannot write outside its folders", f"echo x > '{outside}/b.txt'", False)
        out.append(("…and nothing was written there", not (outside / "b.txt").exists(), ""))
        check("cannot read a folder it was not given", f"cat '{outside}/secret.txt'", False, lambda t: SECRET not in t)
        check("cannot list your home folder", f"ls -a '{os.path.expanduser('~')}'", False)
        check("reads a folder granted read-only", f"cat '{granted}/ro.txt'", True, lambda t: "readable" in t)
        check("cannot write a folder granted read-only", f"echo x > '{granted}/c.txt'", False)
        check("cannot change a file through a hard link", f"ln '{outside}/secret.txt' link.txt && echo changed >> link.txt", False)
        out.append(("…and the file outside is unchanged", (outside / "secret.txt").read_text() == SECRET + "\n", ""))
        check("cannot read through a symbolic link", f"ln -s '{outside}/secret.txt' sl.txt && cat sl.txt", False,
              lambda t: SECRET not in t)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        connect = f"exec 3<>/dev/tcp/127.0.0.1/{port}"
        check("no network when it is off", connect, False)
        check("no other ports when the network is on", connect, False, net=True)
    finally:
        if listener:
            listener.close()
        shutil.rmtree(root, ignore_errors=True)
    return out


def main(base: Path) -> int:
    results = run_checks(base)
    for name, good, detail in results:
        print(f"{'ok ' if good else 'NO '} sandbox: {name}" + (f" ({detail})" if detail else ""))
    bad = [r for r in results if not r[1]]
    print("The sandbox holds on this machine." if not bad else
          f"{len(bad)} check(s) failed: the sandbox does not hold here, so do not let baabaa run commands on this machine.")
    return 1 if bad else 0

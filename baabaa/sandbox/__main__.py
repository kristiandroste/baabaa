"""python3 -m baabaa.sandbox [--rw DIR]... [--ro DIR]... --net on|off [--ports 80,443] --cwd DIR -- CMD...

Linux: confines itself with Landlock and seccomp, then execs CMD. macOS: execs CMD through sandbox-exec with a
profile for these folders (seatbelt.py). Exits 126 if the sandbox cannot be enforced.
"""

import argparse
import os
import sys

if __package__ in (None, ""):  # run by path: make the package importable without touching the environment
    sys.path[0] = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from baabaa.sandbox import DEV_RW, SYSTEM_RO
else:
    from . import DEV_RW, SYSTEM_RO


def main() -> None:
    ap = argparse.ArgumentParser(prog="baabaa.sandbox")
    ap.add_argument("--rw", action="append", default=[])
    ap.add_argument("--ro", action="append", default=[])
    ap.add_argument("--net", choices=("on", "off"), default="off")
    ap.add_argument("--ports", default="")
    ap.add_argument("--cwd", required=True)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    cmd = args.cmd[1:] if args.cmd and args.cmd[0] == "--" else args.cmd
    if not cmd:
        print("baabaa sandbox: no command", file=sys.stderr)
        sys.exit(2)
    net = args.net == "on"
    ports = [int(p) for p in args.ports.split(",") if p.strip().isdigit()]
    if sys.platform == "darwin":
        from baabaa.sandbox import seatbelt
        if not seatbelt.available():
            print("baabaa sandbox: refusing to run unconfined: sandbox-exec is missing", file=sys.stderr)
            sys.exit(126)
        try:
            os.chdir(args.cwd)
            confined = seatbelt.argv(cmd, args.rw, args.ro, net, ports)
            os.execv(confined[0], confined)
        except OSError as exc:
            print(f"baabaa sandbox: refusing to run unconfined: {exc}", file=sys.stderr)
            sys.exit(126)
    from baabaa.sandbox import landlock, seccomp
    try:
        os.chdir(args.cwd)
        landlock.restrict(SYSTEM_RO + args.ro, DEV_RW + args.rw, net, ports)
        seccomp.install(net)
    except (landlock.LandlockError, seccomp.SeccompError, OSError) as exc:
        print(f"baabaa sandbox: refusing to run unconfined: {exc}", file=sys.stderr)
        sys.exit(126)
    try:
        os.execvp(cmd[0], cmd)
    except OSError as exc:
        print(f"baabaa sandbox: {cmd[0]}: {exc.strerror}", file=sys.stderr)
        sys.exit(127)


if __name__ == "__main__":
    main()

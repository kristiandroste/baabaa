"""A seccomp-BPF filter assembled by hand (no library): default allow, EPERM for chosen syscalls.

Always denied: ptrace, process_vm_readv/writev, io_uring (it can open sockets without socket()),
bpf and userfaultfd. With the network off, socket() is allowed only for AF_UNIX.
"""

import ctypes
import platform
import struct
import sys

PR_SET_SECCOMP, SECCOMP_MODE_FILTER = 22, 2
RET_KILL_PROCESS, RET_ERRNO, RET_ALLOW = 0x80000000, 0x00050000, 0x7FFF0000
EPERM = 1
AF_UNIX = 1

LD_W_ABS, JEQ_K, JGE_K, RET_K = 0x20, 0x15, 0x35, 0x06
OFF_NR, OFF_ARCH, OFF_ARG0 = 0, 4, 16

ARCHES = {
    "x86_64": {"audit": 0xC000003E, "x32": True, "socket": 41, "deny": [101, 310, 311, 425, 426, 427, 321, 323]},
    "aarch64": {"audit": 0xC00000B7, "x32": False, "socket": 198, "deny": [117, 270, 271, 425, 426, 427, 280, 282]},
}


class SeccompError(Exception):
    pass


def _ins(code, jt, jf, k) -> bytes:
    return struct.pack("HBBI", code, jt, jf, k)


def build(net_allowed: bool, machine: str | None = None) -> bytes:
    arch = ARCHES.get(machine or platform.machine())
    if arch is None:
        raise SeccompError(f"no seccomp filter for {machine or platform.machine()}")
    prog = [_ins(LD_W_ABS, 0, 0, OFF_ARCH), _ins(JEQ_K, 1, 0, arch["audit"]), _ins(RET_K, 0, 0, RET_KILL_PROCESS),
            _ins(LD_W_ABS, 0, 0, OFF_NR)]
    if arch["x32"]:
        prog += [_ins(JGE_K, 0, 1, 0x40000000), _ins(RET_K, 0, 0, RET_KILL_PROCESS)]
    for nr in arch["deny"]:
        prog += [_ins(JEQ_K, 0, 1, nr), _ins(RET_K, 0, 0, RET_ERRNO | EPERM)]
    if not net_allowed:
        # if nr == socket: allow only AF_UNIX
        prog += [_ins(JEQ_K, 0, 3, arch["socket"]),
                 _ins(LD_W_ABS, 0, 0, OFF_ARG0),
                 _ins(JEQ_K, 1, 0, AF_UNIX),
                 _ins(RET_K, 0, 0, RET_ERRNO | EPERM)]
    prog.append(_ins(RET_K, 0, 0, RET_ALLOW))
    return b"".join(prog)


def install(net_allowed: bool) -> None:
    if sys.platform != "linux":
        raise SeccompError("seccomp is Linux's")
    program = build(net_allowed)
    count = len(program) // 8
    buf = ctypes.create_string_buffer(program, len(program))

    class SockFprog(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.c_void_p)]

    fprog = SockFprog(count, ctypes.cast(buf, ctypes.c_void_p))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(fprog), 0, 0) != 0:
        raise SeccompError(f"seccomp install failed (errno {ctypes.get_errno()})")

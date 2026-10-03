"""Landlock through ctypes: an unprivileged, allow-list filesystem and TCP-port sandbox (Linux 5.13+)."""

import ctypes
import os
import struct
import sys

SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446
CREATE_RULESET_VERSION = 1
RULE_PATH_BENEATH, RULE_NET_PORT = 1, 2
PR_SET_NO_NEW_PRIVS = 38

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
REMOVE_DIR, REMOVE_FILE, MAKE_CHAR, MAKE_DIR = 1 << 4, 1 << 5, 1 << 6, 1 << 7
MAKE_REG, MAKE_SOCK, MAKE_FIFO, MAKE_BLOCK, MAKE_SYM = 1 << 8, 1 << 9, 1 << 10, 1 << 11, 1 << 12
REFER, TRUNCATE, IOCTL_DEV = 1 << 13, 1 << 14, 1 << 15
NET_BIND_TCP, NET_CONNECT_TCP = 1 << 0, 1 << 1

_FS_BY_ABI = {1: (1 << 13) - 1, 2: (1 << 14) - 1, 3: (1 << 15) - 1, 4: (1 << 15) - 1, 5: (1 << 16) - 1}
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV
READ_RIGHTS = EXECUTE | READ_FILE | READ_DIR

_libc = ctypes.CDLL(None, use_errno=True)
_libc.syscall.restype = ctypes.c_long


class LandlockError(Exception):
    pass


def _syscall(nr, *args) -> int:
    res = _libc.syscall(ctypes.c_long(nr), *args)
    if res < 0:
        err = ctypes.get_errno()
        raise LandlockError(f"syscall {nr} failed: {os.strerror(err)}")
    return res


def abi_version() -> int:
    if sys.platform != "linux":  # these syscall numbers mean something else elsewhere
        return 0
    res = _libc.syscall(ctypes.c_long(SYS_CREATE_RULESET), None, ctypes.c_size_t(0), ctypes.c_uint32(CREATE_RULESET_VERSION))
    return int(res) if res > 0 else 0


def restrict(read_only: list[str], read_write: list[str], net_allowed: bool, connect_ports=()) -> int:
    """Confine this process (and later children). Returns the Landlock ABI used. Raises if unenforced."""
    abi = abi_version()
    if abi < 1:
        raise LandlockError("Landlock is not available on this kernel")
    fs_all = _FS_BY_ABI.get(abi, _FS_BY_ABI[5])
    handled_net = 0
    if abi >= 4:
        handled_net = NET_CONNECT_TCP if net_allowed else (NET_BIND_TCP | NET_CONNECT_TCP)
    if handled_net:
        attr = struct.pack("QQ", fs_all, handled_net)
    else:
        attr = struct.pack("Q", fs_all)
    buf = ctypes.create_string_buffer(attr, len(attr))
    ruleset_fd = _syscall(SYS_CREATE_RULESET, buf, ctypes.c_size_t(len(attr)), ctypes.c_uint32(0))
    try:
        for path, rights in [(p, READ_RIGHTS) for p in read_only] + [(p, fs_all) for p in read_write]:
            _add_path(ruleset_fd, path, rights & fs_all)
        if handled_net and net_allowed:
            for port in connect_ports:
                rule = struct.pack("QQ", NET_CONNECT_TCP, int(port))
                rbuf = ctypes.create_string_buffer(rule, len(rule))
                _syscall(SYS_ADD_RULE, ctypes.c_int(ruleset_fd), ctypes.c_int(RULE_NET_PORT), rbuf, ctypes.c_uint32(0))
        if _libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise LandlockError("prctl(NO_NEW_PRIVS) failed")
        _syscall(SYS_RESTRICT_SELF, ctypes.c_int(ruleset_fd), ctypes.c_uint32(0))
    finally:
        os.close(ruleset_fd)
    return abi


def _add_path(ruleset_fd: int, path: str, rights: int) -> None:
    try:
        fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return  # a missing system directory is simply not granted
    try:
        if not os.path.isdir(path):
            rights &= FILE_RIGHTS
        rule = struct.pack("=Qi", rights, fd)  # packed: u64 allowed_access, s32 parent_fd
        rbuf = ctypes.create_string_buffer(rule, len(rule))
        _syscall(SYS_ADD_RULE, ctypes.c_int(ruleset_fd), ctypes.c_int(RULE_PATH_BENEATH), rbuf, ctypes.c_uint32(0))
    finally:
        os.close(fd)

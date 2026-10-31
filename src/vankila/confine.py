"""Limit what smolvm's VM process can reach on the host, using Landlock.

smolvm runs as the invoking user, so a VMM bug could otherwise read anything the
user can, such as ~/.ssh. Landlock restrictions can't be undone and are inherited
by children, so applying them just before exec covers the VMM it starts.

Also run directly as a wrapper, with `python3 -I`, so this file imports only the
standard library: confine.py SPEC_JSON COMMAND [ARGS...]. It's a separate process
because `vk` has threads running, which makes applying this in a fork unsafe.
"""

from __future__ import annotations

import ctypes
import json
import os
import struct
import sys

_SYS_CREATE_RULESET, _SYS_ADD_RULE, _SYS_RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1
_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38

_EXECUTE, _WRITE_FILE, _READ_FILE, _READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
_REMOVE_DIR, _REMOVE_FILE, _MAKE_CHAR, _MAKE_DIR = 1 << 4, 1 << 5, 1 << 6, 1 << 7
_MAKE_REG, _MAKE_SOCK, _MAKE_FIFO, _MAKE_BLOCK = 1 << 8, 1 << 9, 1 << 10, 1 << 11
_MAKE_SYM, _REFER, _TRUNCATE, _IOCTL_DEV = 1 << 12, 1 << 13, 1 << 14, 1 << 15

_FILE_RIGHTS = _EXECUTE | _WRITE_FILE | _READ_FILE | _TRUNCATE | _IOCTL_DEV
_READ_RIGHTS = _EXECUTE | _READ_FILE | _READ_DIR
_WRITE_RIGHTS = (
    _READ_RIGHTS | _WRITE_FILE | _REMOVE_DIR | _REMOVE_FILE | _MAKE_CHAR | _MAKE_DIR | _MAKE_REG
    | _MAKE_SOCK | _MAKE_FIFO | _MAKE_BLOCK | _MAKE_SYM | _REFER | _TRUNCATE | _IOCTL_DEV
)


def _syscall(number: int, *args) -> int:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(ctypes.c_long(number), *args)
    if result < 0:
        raise OSError(ctypes.get_errno(), os.strerror(ctypes.get_errno()))
    return result


def abi_version() -> int:
    """The kernel's Landlock version, or 0 if it's missing or turned off."""
    try:
        return _syscall(_SYS_CREATE_RULESET, None, ctypes.c_size_t(0),
                        ctypes.c_uint32(_CREATE_RULESET_VERSION))
    except OSError:
        return 0


def _handled_rights(abi: int) -> int:
    rights = _WRITE_RIGHTS
    if abi < 2:
        rights &= ~_REFER
    if abi < 3:
        rights &= ~_TRUNCATE
    if abi < 5:
        rights &= ~_IOCTL_DEV
    return rights


def apply(read_only: list[str], read_write: list[str]) -> None:
    """Restrict this process and its future children to these paths."""
    abi = abi_version()
    if abi < 1:
        raise OSError("Landlock is not available")
    handled = _handled_rights(abi)
    attr = struct.pack("=Q", handled)
    ruleset = _syscall(_SYS_CREATE_RULESET, ctypes.c_char_p(attr), ctypes.c_size_t(len(attr)),
                       ctypes.c_uint32(0))
    for paths, rights in ((read_only, _READ_RIGHTS), (read_write, _WRITE_RIGHTS)):
        for path in paths:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # Not on this host, so there's nothing to grant.
            allowed = rights & handled
            if not os.path.isdir(path):
                allowed &= _FILE_RIGHTS
            rule = struct.pack("=Qi", allowed, fd)
            _syscall(_SYS_ADD_RULE, ctypes.c_int(ruleset), ctypes.c_int(_RULE_PATH_BENEATH),
                     ctypes.c_char_p(rule), ctypes.c_uint32(0))
            os.close(fd)
    if ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "could not set no_new_privs")
    _syscall(_SYS_RESTRICT_SELF, ctypes.c_int(ruleset), ctypes.c_uint32(0))
    os.close(ruleset)


def wrapper_command(spec: dict[str, list[str]], command: list[str]) -> list[str]:
    return [sys.executable, "-I", os.path.abspath(__file__), json.dumps(spec), *command]


if __name__ == "__main__":
    spec = json.loads(sys.argv[1])
    apply(spec["read_only"], spec["read_write"])
    os.execv(sys.argv[2], sys.argv[2:])

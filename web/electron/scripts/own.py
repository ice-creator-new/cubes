"""Run a program as its own responsible app on macOS: `own.py <program> [args…]`.

macOS checks screen recording and accessibility against a process's responsible app. Started
from a terminal, that is the terminal, so the backend's screenshots were checked against the
terminal's permission and allowing Cubes in System Settings changed nothing. This replaces itself
with the program (same pid, stdio and environment) with the responsibility disclaimed, which is
what Chromium does for its helpers; Electron and the backend it starts then ask in their own name.
Off macOS, or if the call is unavailable, it simply execs the program.
"""

from __future__ import annotations

import ctypes
import os
import sys

POSIX_SPAWN_SETEXEC = 0x0040


def main() -> None:
    argv = sys.argv[1:]
    if not argv:
        raise SystemExit("用法：own.py <程序> [参数…]")
    if sys.platform == "darwin":
        try:
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
            attr = ctypes.c_void_p()
            if libc.posix_spawnattr_init(ctypes.byref(attr)) == 0:
                if (
                    libc.posix_spawnattr_setflags(ctypes.byref(attr), ctypes.c_short(POSIX_SPAWN_SETEXEC)) == 0
                    and libc.responsibility_spawnattrs_setdisclaim(ctypes.byref(attr), 1) == 0
                ):
                    args = (ctypes.c_char_p * (len(argv) + 1))(*[os.fsencode(a) for a in argv], None)
                    env = [os.fsencode(f"{k}={v}") for k, v in os.environ.items()]
                    envp = (ctypes.c_char_p * (len(env) + 1))(*env, None)
                    pid = ctypes.c_int()
                    sys.stdout.flush()
                    sys.stderr.flush()
                    # Returns only on failure: on success this process is already the program.
                    libc.posix_spawn(ctypes.byref(pid), os.fsencode(argv[0]), None, ctypes.byref(attr), args, envp)
        except (AttributeError, OSError):
            pass
    os.execv(argv[0], argv)


if __name__ == "__main__":
    main()

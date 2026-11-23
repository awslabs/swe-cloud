"""Run an unprivileged build and own descendant cleanup for builds and verification."""

import ctypes
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path


def reap_children():
    deadline = time.monotonic() + 5
    parent = os.getpid()
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        # Use stat's PPid when the optional /proc/.../children file is unavailable.
        for entry in Path("/proc").glob("[0-9]*/stat"):
            try:
                ppid = int(entry.read_text().rsplit(")", 1)[1].split()[1])
            except (FileNotFoundError, ProcessLookupError):
                continue
            if ppid != parent:
                continue
            # An unreaped direct child cannot have its PID reused by another process.
            with suppress(ProcessLookupError):
                os.kill(int(entry.parent.name), signal.SIGKILL)
        if time.monotonic() >= deadline:
            raise RuntimeError("Candidate descendants survived cleanup; refusing to score")
        time.sleep(0.01)


def isolate_children():
    libc = ctypes.CDLL(None, use_errno=True)
    for option in (36, 38):  # PR_SET_CHILD_SUBREAPER and PR_SET_NO_NEW_PRIVS, inherited across exec.
        if libc.prctl(option, 1, 0, 0, 0) != 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))


def main():
    isolate_children()
    try:
        return subprocess.run(
            ["setpriv", "--reuid=2000", "--regid=2000", "--clear-groups", "--no-new-privs", "bash", "-c", "cd /app && bash build.sh"],
            stdin=subprocess.DEVNULL,
            env={**os.environ, "HOME": "/home/candidate"},
            timeout=900,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired:
        return 124
    finally:
        reap_children()


if __name__ == "__main__":
    sys.exit(main())

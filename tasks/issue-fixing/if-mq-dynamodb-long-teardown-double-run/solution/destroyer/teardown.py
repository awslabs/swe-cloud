"""Run vm-destroy for one VM as a child process and follow its output."""

import logging
import os
import select
import subprocess

log = logging.getLogger("destroyer.teardown")


def run_destroy(vm_id: str, on_progress=None, idle=None, poll_s: float = 1.0) -> int:
    """Run ``vm-destroy --vm-id <vm_id>`` to completion and return its exit status.

    ``on_progress(n)`` is called for every ``progress <n>`` line; ``idle()`` is called at least every
    ``poll_s`` seconds while the child runs, so the caller can keep its connections serviced. If
    ``idle()`` returns False the child is killed and its exit status returned.
    """
    child = subprocess.Popen(["vm-destroy", "--vm-id", vm_id], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    pending = b""
    try:
        while True:
            readable, _, _ = select.select([child.stdout], [], [], poll_s)
            if readable:
                chunk = os.read(child.stdout.fileno(), 4096)
                if not chunk:
                    break
                pending += chunk
                *lines, pending = pending.split(b"\n")
                for raw in lines:
                    line = raw.decode(errors="replace").strip()
                    log.info("%s: %s", vm_id, line)
                    if line.startswith("progress ") and on_progress is not None:
                        on_progress(int(line.split()[1]))
            if idle is not None and idle() is False:
                log.warning("%s: abandoning vm-destroy pid %s", vm_id, child.pid)
                child.kill()
                break
        return child.wait()
    finally:
        child.stdout.close()

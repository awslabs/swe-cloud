#!/usr/bin/env bash
# tests/test.sh: the verifier owns the reward file and never leaves it unwritten.
set -uo pipefail
mkdir -p /logs/verifier
rm -f /logs/verifier/reward.txt /logs/verifier/reward.json

# The solve phase ran in this container before the verifier and may have left
# background workers behind — fault proxies, self-test loops, stray aft-sync
# runs. Whatever they do to the account-factory endpoint during grading would
# be graded as if the submission had done it, so clear them first: every
# process holding a connection to the endpoint, together with the process tree
# it belongs to, sparing the endpoint itself, this script's own chain, and the
# platform's own probes of the endpoint. Nothing else in the container is
# touched, so a daemon the platform runs is left alone.
python3 - <<'PY' || true
import os
import signal
import time

ENDPOINT_PORT = 4566
LOOPBACK = ("0100007f", "0" * 31 + "1")
QUIET_WINDOW_S = 10.0        # this much silence before concluding
SWEEP_LIMIT_S = 45.0         # overall cap
ROUND_SLEEP = 0.2


def read(path):
    try:
        with open(path) as fh:
            return fh.read()
    except OSError:
        return None


def link(path):
    try:
        return os.readlink(path)
    except OSError:
        return None


def ppid_of(pid):
    """A process's parent. The comm field may contain spaces, so the stat line
    is split after its closing parenthesis."""
    stat = read("/proc/%d/stat" % pid)
    if stat is None:
        return None
    tail = stat.rsplit(")", 1)
    if len(tail) != 2 or len(tail[1].split()) < 2:
        return None
    try:
        return int(tail[1].split()[1])
    except ValueError:
        return None


def cmdline(pid):
    raw = read("/proc/%d/cmdline" % pid)
    return "" if raw is None else " ".join(raw.split("\0"))


def ancestry(pid, spare):
    """The parent chain of pid, cut at any process in `spare`."""
    chain = []
    seen = set()
    current = pid
    while current not in seen and len(chain) < 24:
        parent = ppid_of(current)
        if not parent or parent in spare:
            break
        chain.append(parent)
        seen.add(parent)
        current = parent
    return chain


def process_tree():
    """(every pid, ppid -> children) for the container."""
    pids = []
    children = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        pids.append(pid)
        parent = ppid_of(pid)
        if parent:
            children.setdefault(parent, []).append(pid)
    return pids, children


def subtree(pid, children):
    found = []
    stack = [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child)
    return found


def endpoint_tree():
    """The endpoint binary and anything it forked."""
    tree = set()
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        comm = (read("/proc/%d/comm" % pid) or "").strip()
        exe = link("/proc/%d/exe" % pid) or ""
        if comm == "floci" or "floci" in exe:
            tree.add(pid)
    _, children = process_tree()
    stack = list(tree)
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in tree:
                tree.add(child)
                stack.append(child)
    return tree


def endpoint_sockets():
    """Inodes of established loopback connections to the endpoint."""
    inodes = set()
    for table in ("/proc/net/tcp", "/proc/net/tcp6"):
        raw = read(table)
        if raw is None:
            continue
        for line in raw.splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "01":
                continue
            address, _, port = fields[2].partition(":")
            try:
                connected = int(port, 16) == ENDPOINT_PORT
            except ValueError:
                continue
            if connected and address.lower() in LOOPBACK:
                inodes.add(fields[9])
    return inodes


def holders(inodes):
    """Processes with one of those sockets open."""
    found = set()
    if not inodes:
        return found
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            names = os.listdir("/proc/%d/fd" % pid)
        except OSError:
            continue
        for name in names:
            target = link("/proc/%d/fd/%s" % (pid, name))
            if target and target.startswith("socket:[") and target[8:-1] in inodes:
                found.add(pid)
                break
    return found


def platform_probe(pid, chain):
    """True for the platform's own checks of the endpoint, which must be left
    running: a probe launched by the endpoint's start script, or a plain fetch
    of it, which cannot change anything in the endpoint."""
    if os.path.basename(link("/proc/%d/exe" % pid) or "") == "curl":
        return True
    return any("start_floci" in cmdline(member) for member in chain)


spare = {1, os.getpid(), os.getppid()}
spare |= set(ancestry(os.getpid(), ()))
endpoint = endpoint_tree()
spare |= endpoint

cleared = []
started = time.monotonic()
last_seen = time.monotonic()
while time.monotonic() - started < SWEEP_LIMIT_S:
    intruders = holders(endpoint_sockets()) - spare - endpoint
    _, children = process_tree()
    trees = []
    for pid in intruders:
        chain = [pid] + ancestry(pid, spare)
        if platform_probe(pid, chain):
            continue
        trees.append(chain + subtree(pid, children))
    if trees:
        last_seen = time.monotonic()
        for pid in sorted({member for tree in trees for member in tree} - spare):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                continue
            cleared.append((pid, (read("/proc/%d/comm" % pid) or "?").strip()))
    elif time.monotonic() - last_seen >= QUIET_WINDOW_S:
        break
    time.sleep(ROUND_SLEEP)

if cleared:
    seen = {}
    for pid, comm in cleared:
        seen.setdefault((pid, comm), 0)
        seen[(pid, comm)] += 1
    print("cleared leftover solve-phase processes:",
          ", ".join("%d(%s)" % pair for pair in sorted(seen)))
PY

bash /usr/local/bin/start_floci.sh || { echo 0 > /logs/verifier/reward.txt; echo environment > /logs/verifier/marker.txt; exit 1; }
command -v runuser >/dev/null 2>&1 || { echo 0 > /logs/verifier/reward.txt; echo environment > /logs/verifier/marker.txt; exit 1; }
python3 -m pytest -q -p no:cacheprovider /tests/test_*.py > /logs/verifier/tests.txt 2>&1
rc=$?
cat /logs/verifier/tests.txt
if [ "$rc" -eq 0 ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
exit "$rc"

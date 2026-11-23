"""Trusted test utilities; candidate processes run as uid 2000."""

import os
import stat
import subprocess
import sys
import tempfile
import threading
import xml.etree.ElementTree as ET
from contextlib import contextmanager, suppress
from pathlib import Path


@contextmanager
def workspace():
    """A private writable directory accessible to both test and candidate."""
    with tempfile.TemporaryDirectory(prefix="reconstruction-") as directory:
        os.chown(directory, 2000, 2000)
        yield Path(directory)


def _status(path=Path("/proc/self/status")):
    """A /proc status file's fields."""
    return dict(line.split(":", 1) for line in path.read_text().splitlines() if ":" in line)


def _held_capabilities():
    """The permitted, effective and ambient capability sets the calling thread holds, nonzero ones only.

    Capabilities are per thread, and a child forks from the caller, not the process leader. Unreadable counts as held.
    """
    for status in (Path("/proc/thread-self/status"), Path(f"/proc/self/task/{threading.get_native_id()}/status")):
        try:
            fields = _status(status)
        except OSError:
            continue
        return {name: fields[name].strip() for name in ("CapPrm", "CapEff", "CapAmb") if int(fields[name], 16)}
    return {"status": "unreadable"}


def _candidate_options(options):
    """Keep child processes inside the candidate identity, including generated code."""
    # A host import of this package gets ProgramBench's adapter; shipped copies get their pipeline's.
    if __package__:
        from .runtime_adapter import CANDIDATE_ENV
    else:
        from runtime_adapter import CANDIDATE_ENV

    forbidden = {"user", "group", "extra_groups", "preexec_fn", "pass_fds"} & options.keys()
    if forbidden:
        raise ValueError(f"Candidate credentials and inherited descriptors are fixed: {sorted(forbidden)}")
    # Root switches to the candidate; a caller already wholly the candidate (ReversePrep's verifier) stays. Any mixed identity
    # is refused: a real or saved uid 0, gid or group 0, or a held capability would pass root's power on to the child.
    if os.geteuid() == 0:
        credentials = {"user": 2000, "group": 2000, "extra_groups": []}
    elif os.getresuid() == os.getresgid() == (2000, 2000, 2000) and set(os.getgroups()) <= {2000} and not _held_capabilities():
        credentials = {}
    else:
        raise PermissionError(
            f"Candidate commands run from root or wholly as uid and gid 2000 without capabilities, not uids {os.getresuid()}, "
            f"gids {os.getresgid()}, groups {os.getgroups()}, capabilities {_held_capabilities()}"
        )
    return {
        **options,
        "cwd": options.get("cwd") or "/app",
        "env": {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/home/candidate",
            "LANG": "C.UTF-8",
            **CANDIDATE_ENV,
            **(options.get("env") or {}),
        },
        **credentials,
        "close_fds": True,
    }


def run_command(command, **options):
    """Run a command or candidate-produced script/binary as the candidate."""
    if options.get("input") is None and "stdin" not in options:
        options["stdin"] = subprocess.DEVNULL
    return subprocess.run(command, **_candidate_options(options))


def start_command(command, **options):
    """Start a candidate process; the caller owns its termination and wait."""
    options.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.Popen(command, **_candidate_options(options))


def run_program(args, *, cwd=None, stdin=None, timeout=30, env=None, text=True):
    """Run /app/program as the candidate; use text=False for byte-oriented I/O."""
    return run_command(
        ["/app/program", *map(str, args)],
        cwd=cwd,
        env=env,
        input=stdin,
        text=text,
        capture_output=True,
        timeout=timeout,
    )


def give_to_candidate(path, *, recursive=False):
    """Hand over a scratch path without following symlinks in it or its ancestors.

    Hand a tree over recursively before starting any candidate process: each directory is checked as the walk reaches
    it, but a candidate that starts and exits between two checks, moving a name, is not seen.
    """
    chown_fixture(path, 2000, 2000, recursive=recursive)


BATCH = 256  # files of one directory held open per recheck of it


def chown_fixture(path, uid, gid, *, recursive=False, follow_symlinks=False):
    """Change fixture ownership through descriptors, in one walk; never follow links.

    A file with several names goes over only when every name is in the tree handed over and no running process but this
    one can write a directory on the way, so no name moves mid-walk to be counted twice: another name may be a root-owned
    binary the candidate linked in to be given it. Inside a tree such a file is left as it is; named alone, refused. The
    same holds for a file with one link: one unlinked between open and fstat leaves a root file's own name as that link.
    A file not already the target's is checked again before it changes hands, once per batch of at most BATCH files of a
    directory and after all of them are opened, so a candidate started mid-walk that is still running then is seen; a
    batch already the target's skips that scan of running processes.
    """
    if __package__:
        from . import fixture_io
    else:
        import fixture_io

    if follow_symlinks:
        raise ValueError("Fixture ownership must not follow symlinks")
    path = Path(os.path.abspath(path))  # noqa: PTH100 -- resolving first would follow candidate links
    scratch = (Path("/app"), Path("/work"), Path("/home/candidate"))
    roots = (Path(tempfile.gettempdir()), Path(tempfile.gettempdir()).resolve(), *scratch)
    root = next((root for root in roots if root in path.parents or (root == path and root in scratch)), None)
    if root is None:
        raise ValueError(f"Ownership changes require a path inside a candidate scratch directory: {path}")

    def settled(directory):
        # Handing a file wholly to root gives nothing away. Asked for each directory, so a candidate started mid-walk counts.
        return (uid, gid) == (0, 0) or fixture_io.settled(directory)

    def still(opened, directories):
        # Asked again just before a change, so a candidate started since the directory was checked counts: unless the
        # file already is the target's, a running candidate's unlink between open and fstat may have left another's file.
        return (opened.st_uid, opened.st_gid) == (uid, gid) or all(settled(directory) for directory in directories)

    names = {}
    folders = {}
    pending = {}

    def once(question):
        answers = []
        return lambda: answers[0] if answers else answers.append(question()) or answers[0]

    def flush(parent, batch):
        # One scan after every file in the batch was opened and stat'ed covers them all; never reused for a later batch.
        try:
            if all((opened.st_uid, opened.st_gid) == (uid, gid) for _, opened in batch) or settled(os.fstat(parent)):
                for descriptor, _ in batch:
                    change(descriptor)
            else:
                for descriptor, opened in batch:
                    if (opened.st_uid, opened.st_gid) == (uid, gid):
                        change(descriptor)
        finally:
            for descriptor, _ in batch:
                os.close(descriptor)
            batch.clear()

    def change(descriptor):
        if hasattr(os, "O_PATH"):
            os.chown(f"/proc/self/fd/{descriptor}", uid, gid)  # the opened file, whatever its name now names
        else:
            os.fchown(descriptor, uid, gid)

    def hand_over(parent, name, trusted, top, batch=None, above=None):
        # trusted: whether the directory holding name and its ancestors are settled; above: only its ancestors. Both are
        # asked when first needed, so a directory of single-link files costs one scan, its batch's.
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            return
        if stat.S_ISDIR(info.st_mode):
            if batch:  # held descriptors stay bounded however deep the tree
                flush(parent, batch)
            descriptor = fixture_io.open_directory(name, dir_fd=parent)
            children = []
            try:
                # Children are handed over first, so a directory stays root's while it is walked.
                if recursive:
                    inside = once(lambda: trusted() and settled(os.fstat(descriptor)))
                    for child in os.listdir(descriptor):  # noqa: PTH208 -- retain the checked directory descriptor
                        hand_over(descriptor, child, inside, top=False, batch=children, above=trusted)
                    flush(descriptor, children)
                os.fchown(descriptor, uid, gid)
            finally:
                for held, _ in children:
                    os.close(held)
                os.close(descriptor)
            return
        descriptor = os.open(name, getattr(os, "O_PATH", os.O_RDONLY | os.O_NONBLOCK) | os.O_NOFOLLOW, dir_fd=parent)
        try:
            opened = os.fstat(descriptor)
            if stat.S_ISLNK(opened.st_mode):
                return
            # Whatever its link count, a file another process can rename in or out of its directory may be a root file's:
            # a single-link one is judged with its batch, after it is opened, by settled(its directory) then.
            if top:
                if not settled(os.fstat(parent)):
                    raise ValueError(f"Refusing to hand over {path}: a running process other than this one can rename it")
            elif not (trusted() if opened.st_nlink > 1 else above()):
                return
            if opened.st_nlink > 1:
                if top:
                    raise ValueError(f"Refusing to hand over {path}: it has hard links outside it")
                key, directory = (opened.st_dev, opened.st_ino), os.fstat(parent)
                names.setdefault(key, set()).add((directory.st_dev, directory.st_ino, name))
                folders.setdefault(key, []).append(directory)
                if key not in pending:
                    pending[key], descriptor = descriptor, None
                # One descriptor per file, released as soon as every link has been seen.
                if len(names[key]) == os.fstat(pending[key]).st_nlink:
                    if still(os.fstat(pending[key]), folders[key]):
                        change(pending[key])
                    os.close(pending.pop(key))
                return
            if not top:
                batch.append((descriptor, opened))
                descriptor = None
                if len(batch) >= BATCH:
                    flush(parent, batch)
            elif still(opened, [os.fstat(parent)]):
                change(descriptor)
            else:
                raise ValueError(f"Refusing to hand over {path}: a process started that can rename it")
        finally:
            if descriptor is not None:
                os.close(descriptor)

    try:
        with fixture_io.parent(path) as (descriptor, name):
            hand_over(descriptor, name, lambda: recursive, top=True, above=lambda: recursive)
    finally:
        for descriptor in pending.values():
            os.close(descriptor)


def write_fixture(path, data, *, encoding="utf-8", mode=None):
    """Write a candidate-owned fixture through checked directory descriptors."""
    if __package__:
        from . import fixture_io
    else:
        import fixture_io

    path = Path(os.path.abspath(path))  # noqa: PTH100 -- do not resolve candidate links
    missing = []
    ancestor = path.parent
    while not ancestor.exists():
        missing.append(ancestor)
        ancestor = ancestor.parent
    fixture_io.mkdir(path.parent, parents=True, exist_ok=True)
    payload = data.encode(encoding) if isinstance(data, str) else data
    # A new file, handed over through its own descriptor before it takes the name: a file the name held may be root's.
    with fixture_io.parent(path) as (directory, name):
        try:
            previous = os.stat(name, dir_fd=directory, follow_symlinks=False)
        except FileNotFoundError:
            previous = None
        if mode is None:
            mode = previous.st_mode & 0o777 if previous is not None and stat.S_ISREG(previous.st_mode) else 0o666 & ~_umask()
        temporary = f".{name}.{os.urandom(8).hex()}"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        try:
            with os.fdopen(os.dup(descriptor), "wb") as stream:
                stream.write(payload)
            os.fchown(descriptor, 2000, 2000)
            os.fchmod(descriptor, mode)
            os.rename(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
        except BaseException:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
            raise
        finally:
            os.close(descriptor)
    for directory in reversed(missing):
        give_to_candidate(directory)
    return len(data)


def _umask():
    """This process's umask, read without changing it."""
    try:
        return int(_status()["Umask"], 8)
    except (OSError, KeyError):
        return 0o022


def reward(pytest_status: int, report: Path) -> float:
    """The fraction of test cases that passed, skipped cases not counted; exactly 1 only when the whole pytest run passed."""
    entries = list(ET.parse(report).iter("testcase"))
    # pytest reports a teardown error after a failed call as a second entry for the same case.
    outcomes = {}
    for entry in entries:
        tags = {tag for tag in ("failure", "error", "skipped") if entry.find(tag) is not None}
        outcomes.setdefault((entry.get("classname"), entry.get("name")), set()).update(tags)
    counted = [tags for tags in outcomes.values() if tags != {"skipped"}]
    passed = sum(1 for tags in counted if not tags)
    if pytest_status == 0 and outcomes and all(not tags for tags in outcomes.values()):
        return 1.0
    return passed / len(counted) if passed < len(counted) else 0.0


if __name__ == "__main__":
    sys.stdout.write(f"{reward(int(sys.argv[1]), Path(sys.argv[2]))}\n")

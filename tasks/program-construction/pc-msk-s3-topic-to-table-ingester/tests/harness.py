"""Trusted test utilities; candidate processes run as uid 2000."""

import os
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def workspace():
    """A private writable directory accessible to both test and candidate."""
    with tempfile.TemporaryDirectory(prefix="reconstruction-") as directory:
        os.chown(directory, 2000, 2000)
        yield Path(directory)


# The venue's fixed endpoint and local credentials; tests pass engine endpoints explicitly.
VENUE = {
    "AWS_ENDPOINT_URL": "http://engines:4566",
    "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_REGION": "us-east-1",
    "AWS_ACCESS_KEY_ID": "test",
    "AWS_SECRET_ACCESS_KEY": "test",
    "AWS_EC2_METADATA_DISABLED": "true",
}


def _candidate_options(options):
    """Keep child processes inside the candidate identity, including generated code."""
    forbidden = {"user", "group", "extra_groups", "preexec_fn", "pass_fds"} & options.keys()
    if forbidden:
        raise ValueError(f"Candidate credentials and inherited descriptors are fixed: {sorted(forbidden)}")
    return {
        **options,
        "cwd": options.get("cwd") or "/app",
        "env": {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": "/home/candidate",
            "LANG": "C.UTF-8",
            **VENUE,
            **(options.get("env") or {}),
        },
        "user": 2000,
        "group": 2000,
        "extra_groups": [],
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
    """Hand over a scratch path without following symlinks in it or its ancestors."""
    chown_fixture(path, 2000, 2000, recursive=recursive)


def chown_fixture(path, uid, gid, *, recursive=False, follow_symlinks=False):
    """Change fixture ownership through directory descriptors; never follow links."""
    if __package__:
        from . import fixture_io
    else:
        import fixture_io

    if follow_symlinks:
        raise ValueError("Fixture ownership must not follow symlinks")
    path = Path(os.path.abspath(path))  # noqa: PTH100 -- resolving first would follow candidate links
    roots = (Path(tempfile.gettempdir()), Path(tempfile.gettempdir()).resolve(), Path("/app"), Path("/work"), Path("/home/candidate"))
    root = next((root for root in roots if root in path.parents), None)
    if root is None:
        raise ValueError(f"Ownership changes require a path inside a candidate scratch directory: {path}")

    def hand_over(parent, name):
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if stat.S_ISLNK(info.st_mode):
            return
        if stat.S_ISDIR(info.st_mode):
            descriptor = fixture_io.open_directory(name, dir_fd=parent)
            try:
                os.fchown(descriptor, uid, gid)
                if recursive:
                    for child in os.listdir(descriptor):  # noqa: PTH208 -- retain the checked directory descriptor
                        hand_over(descriptor, child)
            finally:
                os.close(descriptor)
        else:
            os.chown(name, uid, gid, dir_fd=parent, follow_symlinks=False)

    with fixture_io.parent(path) as (descriptor, name):
        hand_over(descriptor, name)


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
    fixture_io.write_bytes(path, payload)
    if mode is not None:
        fixture_io.chmod(path, mode)
    for directory in reversed(missing):
        give_to_candidate(directory)
    give_to_candidate(path)
    return len(data)


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

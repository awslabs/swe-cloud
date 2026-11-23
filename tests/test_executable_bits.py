"""Every tracked script and program is executable in git, so a fresh clone gets runnable tasks.

Task uploads have repeatedly lost the executable bit (zip tools, S3 and other copies that drop file modes).
Harbor and the task Dockerfiles chmod some scripts themselves, but helpers a test calls directly do not get
that treatment, and other runners may not chmod at all. A file whose first line starts with `#!` is meant to
be run, and so is an ELF program (the program-construction reference programs; shared libraries excluded),
so git must record both as 100755. The check reads the working tree, so run it after `git lfs pull`: an LFS pointer is plain text.
"""

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _tracked_modes() -> dict[str, str]:
    try:
        out = subprocess.run(["git", "ls-files", "-s", "-z"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    except (FileNotFoundError, subprocess.CalledProcessError):
        pytest.skip("not a git checkout")
    modes = {}
    for entry in filter(None, out.split("\0")):
        meta, path = entry.split("\t", 1)
        modes[path] = meta.split()[0]
    return modes


def test_scripts_are_executable_in_git():
    missing = []
    for path, mode in _tracked_modes().items():
        file = ROOT / path
        if mode != "100644" or not file.is_file():
            continue
        with file.open("rb") as fh:
            head = fh.read(4)
        # Shared libraries are ELF too, but they are loaded, not run.
        is_program = head == b"\x7fELF" and not (file.name.endswith(".so") or ".so." in file.name)
        if head.startswith(b"#!") or is_program:
            missing.append(path)
    assert not missing, (
        f"{len(missing)} tracked script(s) or program(s) are not executable in git; fix with\n"
        "  git update-index --chmod=+x <file> (or chmod +x <file> && git add <file>)\n  " + "\n  ".join(missing[:20])
    )

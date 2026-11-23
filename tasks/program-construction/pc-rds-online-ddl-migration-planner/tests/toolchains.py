"""Record task-image toolchains before an agent harness can install extra dependencies."""

import importlib.metadata
import json
import os
import shutil
import subprocess
import sys

# These are discovery probes, not a language allowlist. Keep unavailable entries explicit.
PROBES = {
    "python3": ["--version"],
    "gcc": ["--version"],
    "g++": ["--version"],
    "clang": ["--version"],
    "clang++": ["--version"],
    "opt": ["--version"],
    "llc": ["--version"],
    "make": ["--version"],
    "node": ["--version"],
    "npm": ["--version"],
    "bun": ["--version"],
    "deno": ["--version"],
    "go": ["version"],
    "rustc": ["--version"],
    "cargo": ["--version"],
    "ghc": ["--version"],
    "cabal": ["--version"],
    "stack": ["--version"],
    "java": ["-version"],
    "javac": ["-version"],
    "ruby": ["--version"],
    "perl": ["-e", "print $^V"],
    "php": ["--version"],
    "dotnet": ["--version"],
    "lua": ["-v"],
    "R": ["--version"],
    "swift": ["--version"],
    "uv": ["--version"],
}


def inventory():
    environment = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/candidate", "LANG": "C.UTF-8"}
    toolchains = {}
    for name, args in PROBES.items():
        executable = shutil.which(name, path=environment["PATH"])
        entry = {"path": executable}
        if executable:
            result = subprocess.run([executable, *args], env=environment, capture_output=True, text=True, timeout=30, check=False)
            if result.returncode == 127:
                # Solver placeholders expose the same absence as the verifier.
                entry["path"] = None
            else:
                entry.update(version=(result.stdout or result.stderr).strip(), returncode=result.returncode)
        toolchains[name] = entry
    return {
        "path": environment["PATH"],
        "toolchains": toolchains,
        "python": {
            "executable": sys.executable,
            "version": sys.version,
            "packages": dict(sorted((dist.metadata["Name"], dist.version) for dist in importlib.metadata.distributions())),
        },
    }


if __name__ == "__main__":
    # Match both build.sh and candidate program execution, including traversal permissions.
    os.chdir("/app")
    os.setgroups([])
    os.setgid(2000)
    os.setuid(2000)
    sys.stdout.write(json.dumps(inventory(), indent=2, sort_keys=True) + "\n")

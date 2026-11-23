"""Own the verifier's children and publish a reward only after they are reaped."""

import subprocess
import sys
from pathlib import Path

# -I excludes the script directory; only this root-owned directory may supply helpers.
sys.path.insert(0, str(Path(__file__).parent))
from build import isolate_children, reap_children
from build import main as build_candidate
from harness import reward


def main(tests=("/tests",), *, plugins=()):
    logs = Path("/logs/verifier")
    logs.mkdir(parents=True, exist_ok=True)
    logs.chmod(0o700)
    Path("/tests").chmod(0o700)
    for name in ("reward.txt", "reward.json", "reward.tmp", "results.xml"):
        (logs / name).unlink(missing_ok=True)
    # Before this process becomes the subreaper, so the venue's endpoint watcher outlives candidate cleanup.
    # A venue that never comes up is an infrastructure failure: no reward is published.
    subprocess.run(["floci-venue", "up"], stdin=subprocess.DEVNULL, timeout=600, check=True)
    isolate_children()
    subprocess.run(["chown", "-hR", "candidate:candidate", "/app"], check=True)
    try:
        built = build_candidate()
        if not built:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-p",
                    "pytest_timeout",
                    "--timeout=60",
                    "-q",
                    "-s",
                    "--junitxml=/logs/verifier/results.xml",
                    *[arg for plugin in plugins for arg in ("-p", plugin)],
                    *tests,
                ],
                cwd="/tests",
                timeout=3600,
                check=False,
            )
    finally:
        reap_children()
    score = 0.0 if built else reward(result.returncode, logs / "results.xml")
    (logs / "reward.tmp").write_text(f"{score}\n")
    (logs / "reward.tmp").replace(logs / "reward.txt")


if __name__ == "__main__":
    main()

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import LOGS, EnvironmentFailure, Harness, log  # noqa: E402


@pytest.fixture(scope="session")
def h():
    try:
        harness = Harness()
    except EnvironmentFailure as error:
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / "environment").write_text(f"environment: {error}\n")
        log(f"environment failure: {error}")
        pytest.exit(f"environment: {error}", returncode=3)
    yield harness
    harness.close_all()

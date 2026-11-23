import json
import os
from pathlib import Path

import pytest

import harness


@pytest.fixture(scope="session")
def rec():
    try:
        record = harness.run_case()
    except harness.EnvironmentDown as exc:
        Path(os.environ.get("VERIFIER_LOGS", "/logs/verifier"), "environment").write_text(str(exc))
        pytest.fail(f"environment: {exc}")
    if not record.ok_start:
        pytest.fail("the workload never started: " + "; ".join(record.errors))
    return record

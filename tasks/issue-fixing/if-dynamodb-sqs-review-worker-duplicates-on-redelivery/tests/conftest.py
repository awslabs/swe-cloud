import os

import pytest

import harness as h


@pytest.fixture(scope="session", autouse=True)
def emulator_preflight():
    if not h.emulator_live():
        os.makedirs(h.LOG_ROOT, exist_ok=True)
        with open(os.path.join(h.LOG_ROOT, "environment"), "w") as fh:
            fh.write("emulator not live at preflight\n")
        pytest.exit("environment: emulator not live", returncode=3)
    yield

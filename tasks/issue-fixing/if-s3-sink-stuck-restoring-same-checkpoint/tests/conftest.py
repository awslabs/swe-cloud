import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))

from harness import run_everything  # noqa: E402


@pytest.fixture(scope="session")
def run():
    return run_everything()

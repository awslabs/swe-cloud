import contextlib

import pytest
from harness import start_command, workspace
from realtime import Deployment, Engines

PROGRAM = "/app/program"


@pytest.fixture(scope="session")
def engines():
    eng = Engines()
    try:
        yield eng.start()
    finally:
        eng.stop()


@contextlib.contextmanager
def deployment(engines, **kw):
    with workspace() as root:
        d = Deployment(engines, str(root), start_command, PROGRAM, **kw)
        try:
            yield d
        finally:
            d.close()


@pytest.fixture
def deploy(engines):
    """Factory for isolated deployments (database, queue, prefixes, sink); all are torn down after the test."""
    with contextlib.ExitStack() as stack:
        yield lambda **kw: stack.enter_context(deployment(engines, **kw))


@pytest.fixture(scope="module")
def shared(engines):
    """One running server per module; tests isolate themselves with fresh apps and channel names."""
    with deployment(engines) as d:
        d.start()
        yield d


def pytest_collection_modifyitems(items):
    """Engine provisioning runs in the first engine-backed test's setup; give it room."""
    for item in items:
        if "engines" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.timeout(600))
            break

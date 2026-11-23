import pytest

from emf_driver import Forwarder, delete_group, logs_client, unique


@pytest.fixture
def fresh_target():
    """Unique group/stream names that do not exist yet; the group is deleted afterwards."""
    group, stream = "/emf-tests/" + unique("new"), unique("stream")
    try:
        yield group, stream
    finally:
        delete_group(group)


@pytest.fixture
def log_target(fresh_target):
    """An existing, empty log group and stream."""
    group, stream = fresh_target
    logs = logs_client()
    logs.create_log_group(logGroupName=group)
    logs.create_log_stream(logGroupName=group, logStreamName=stream)
    return group, stream


@pytest.fixture
def forwarder():
    """Factory starting `program start`; every started process is stopped at teardown."""
    started = []

    def start(group=None, stream=None, *extra, env=None, endpoint_flags=True, ports=None):
        process = Forwarder(group, stream, extra=extra, env=env, endpoint_flags=endpoint_flags, ports=ports)
        started.append(process)
        return process.__enter__()

    try:
        yield start
    finally:
        for process in started:
            process.stop()

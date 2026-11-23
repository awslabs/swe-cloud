"""Service failures injected between the program and Secrets Manager."""

import json
import time

import pytest

from conftest import assert_error, assert_same_document, document, free_port, nested_config

BOOM = {"__type": "InternalServiceError", "message": "injected failure"}
EXPIRE = 2.8


def fail(ops, status, body):
    payload = body if isinstance(body, str) else json.dumps(body)
    return lambda op, params: (status, payload) if op in ops else None


def start(launch, recorder, extra=""):
    port = free_port()
    program = launch(nested_config(port, extra + "[capabilities.secrets_manager.cache]\nttl_seconds = 2\n"), endpoint=recorder.url, port=port)
    recorder.take()
    return program


def only(ops, allowed):
    return bool(ops) and set(ops) <= set(allowed)


def test_server_errors_serve_cached_entry(launch, recorder, secrets, sm):
    program = start(launch, recorder)
    name = secrets.create("cached", SecretString="v1")["Name"]
    first = document(program.query(name))
    recorder.rule = fail({"GetSecretValue", "DescribeSecret"}, 500, BOOM)
    time.sleep(EXPIRE)
    recorder.take()
    assert_same_document(document(program.query(name)), first)
    assert only(recorder.take(), {"DescribeSecret"})
    # The failed check renewed the entry.
    assert_same_document(document(program.query(name)), first)
    assert recorder.take() == []
    # Explicit refreshes and uncached lookups report the failure.
    assert_error(program.query(name, refreshNow="true"), 500, BOOM)
    assert only(recorder.take(), {"GetSecretValue"})
    assert_error(program.query(secrets.name("uncached")), 500, BOOM)
    recorder.rule = None
    assert_same_document(document(program.query(name)), first)


def test_server_error_on_refetch_serves_cached_entry(launch, recorder, secrets, sm):
    program = start(launch, recorder)
    name = secrets.create("refetch", SecretString="v1")["Name"]
    first = document(program.query(name))
    sm.put_secret_value(SecretId=name, SecretString="v2")
    recorder.rule = fail({"GetSecretValue"}, 503, {"__type": "ServiceUnavailable", "message": "try later"})
    time.sleep(EXPIRE)
    recorder.take()
    assert_same_document(document(program.query(name)), first)
    ops = recorder.take()
    assert ops[0] == "DescribeSecret" and only(ops[1:], {"GetSecretValue"})
    recorder.rule = None
    assert document(program.query(name))["SecretString"] == "v1"
    assert document(program.query(name, refreshNow="true"))["SecretString"] == "v2"


def test_unreachable_service(launch, recorder, secrets):
    program = start(launch, recorder)
    name = secrets.create("offline", SecretString="v1")["Name"]
    first = document(program.query(name))
    recorder.close()
    time.sleep(EXPIRE)
    assert_same_document(document(program.query(name)), first)
    connection = {"__type": "ConnectionError", "message": "Read Error"}
    assert_error(program.query(name, refreshNow="true"), 502, connection)
    assert_error(program.query(secrets.name("never-cached")), 502, connection)
    assert_same_document(document(program.query(name)), first)


def test_transient_errors_reported_when_not_ignored(launch, recorder, secrets):
    program = start(launch, recorder, "ignore_transient_errors = false\n")
    name = secrets.create("strict", SecretString="v1")["Name"]
    document(program.query(name))
    recorder.rule = fail({"DescribeSecret"}, 500, BOOM)
    time.sleep(EXPIRE)
    recorder.take()
    assert_error(program.query(name), 500, BOOM)
    assert only(recorder.take(), {"DescribeSecret"})
    assert_error(program.query(name), 500, BOOM)
    recorder.close()
    assert_error(program.query(name), 502, {"__type": "ConnectionError", "message": "Read Error"})


@pytest.mark.parametrize(
    "status, body",
    [
        (403, {"__type": "AccessDeniedException", "message": "not authorized to describe"}),
        (400, {"__type": "DecryptionFailure", "message": "key is disabled"}),
        (429, {"__type": "ThrottlingException", "message": "Rate exceeded"}),
    ],
)
def test_client_errors_pass_through_with_status(launch, recorder, secrets, status, body):
    program = start(launch, recorder)
    name = secrets.create("denied", SecretString="v1")["Name"]
    document(program.query(name))
    recorder.rule = fail({"DescribeSecret", "GetSecretValue"}, status, body)
    time.sleep(EXPIRE)
    # Not a transient failure: the cached value is not served.
    assert_error(program.query(name), status, body)
    assert_error(program.query(secrets.name("absent")), status, body)
    assert_error(program.query(name, refreshNow="true"), status, body)


def test_unusable_error_responses(launch, recorder, secrets):
    program = start(launch, recorder)
    for case, (status, payload) in enumerate([(400, '{"__type":"AccessDeniedException"}'), (500, "<html>gateway</html>"), (404, "")]):
        recorder.rule = lambda op, params, s=status, p=payload: (s, p) if op == "GetSecretValue" else None
        assert_error(program.query(secrets.name(f"odd-{case}")), 500, {"__type": "InternalFailure"})

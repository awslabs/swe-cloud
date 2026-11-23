"""Request handling: authorization, methods, routing, parameter parsing and connection limits."""

import socket
import time

import pytest

from conftest import DEFAULT_HEADER, assert_error, assert_matches, assert_text, document, free_port, nested_config, service_error


def test_ping_needs_no_token(agent):
    assert_text(agent.get("/ping", token=False), 200, "healthy")
    assert_text(agent.get("/ping?probe=1", token=False), 200, "healthy")
    assert_text(agent.get("/ping", {"X-Forwarded-For": "10.0.0.1"}, token=False), 200, "healthy")


def test_token_validation(agent, recorder, secrets, sm):
    # Adapted from integration-tests/tests/security.rs::test_ssrf_token_validation.
    created = secrets.create("guarded", SecretString="guarded")
    path = "/secretsmanager/get?secretId=" + created["Name"]
    for headers in ({}, {DEFAULT_HEADER: "wrong"}, {DEFAULT_HEADER: agent.token + "x"}, {"X-Other-Token": agent.token}):
        assert_text(agent.get(path, headers, token=False), 403, "Bad Token")
        assert_text(agent.get("/v1/" + created["Name"], headers, token=False), 403, "Bad Token")
    assert_text(agent.get("/no/such/route", token=False), 403, "Bad Token")
    assert recorder.take() == []

    expected = sm.get_secret_value(SecretId=created["Name"])
    for headers in (
        {DEFAULT_HEADER: agent.token},
        {DEFAULT_HEADER.lower(): agent.token},
        {"X-Vault-Token": agent.token},
        {DEFAULT_HEADER: "wrong", "X-Vault-Token": agent.token},
    ):
        assert_matches(document(agent.get(path, headers, token=False)), expected)


def test_forwarded_requests_rejected(agent, recorder, secrets):
    # security.rs::test_x_forwarded_for_rejection.
    created = secrets.create("forwarded", SecretString="x")
    path = "/secretsmanager/get?secretId=" + created["Name"]
    assert_text(agent.get(path, {"X-Forwarded-For": "203.0.113.7"}), 400, "Forwarded")
    assert_text(agent.get(path, {"X-Forwarded-For": "203.0.113.7"}, token=False), 400, "Forwarded")
    assert_text(agent.get("/nowhere", {"X-Forwarded-For": "203.0.113.7"}, token=False), 400, "Forwarded")
    assert recorder.take() == []


def test_only_get_is_allowed(agent, recorder, secrets):
    created = secrets.create("methods", SecretString="x")
    path = "/secretsmanager/get?secretId=" + created["Name"]
    for method in ("POST", "PUT", "DELETE", "PATCH"):
        assert_text(agent.request(method, path), 405, "Not allowed")
        assert_text(agent.request(method, "/v1/" + created["Name"]), 405, "Not allowed")
        assert_text(agent.request(method, path, token=False), 403, "Bad Token")
    assert_text(agent.request("POST", "/ping", token=False), 405, "Not allowed")
    assert agent.request("HEAD", path)[0] == 405
    assert recorder.take() == []


def test_unknown_routes(agent, recorder):
    for path in ("/", "/nothing-here", "/secretsmanager/get/", "/secretsmanager", "/v1", "/V1/name", "/pingx"):
        assert_text(agent.get(path), 404, "Not found")
    assert recorder.take() == []


def test_parameter_errors(agent, recorder, secrets):
    created = secrets.create("params", SecretString="x")
    name = created["Name"]
    cases = [
        ("/secretsmanager/get", 400, "missing parameter secretId"),
        ("/secretsmanager/get?secretId=", 400, "missing parameter secretId"),
        ("/secretsmanager/get?versionStage=AWSCURRENT", 400, "missing parameter secretId"),
        (f"/secretsmanager/get?secretId={name}&color=blue", 400, "unknown parameter: color"),
        (f"/secretsmanager/get?SecretId={name}", 400, "unknown parameter: SecretId"),
        (f"/secretsmanager/get?secretId={name}&refreshNow=yes", 400, "invalid refreshNow value"),
        (f"/secretsmanager/get?secretId={name}&refreshNow=", 400, "invalid refreshNow value"),
        (f"/secretsmanager/get?secretId={name}&refreshNow=2", 400, "invalid refreshNow value"),
        (f"/v1/{name}?secretId={name}", 400, "unknown parameter: secretId"),
        (f"/v1/{name}?refreshNow=maybe", 400, "invalid refreshNow value"),
        ("/v1/", 400, "missing secret ID"),
        ("/v1/?versionStage=AWSCURRENT", 400, "missing secret ID"),
    ]
    for path, status, text in cases:
        assert_text(agent.get(path), status, text)
    assert recorder.take() == []


@pytest.mark.parametrize(
    "env, accepted",
    [
        ({"AWS_SESSION_TOKEN": "from-session"}, "from-session"),
        ({"AWS_TOKEN": "from-token", "AWS_SESSION_TOKEN": "from-session"}, "from-token"),
        ({"AWS_CONTAINER_AUTHORIZATION_TOKEN": "from-container"}, "from-container"),
        ({"AWS_SESSION_TOKEN": "from-session", "AWS_CONTAINER_AUTHORIZATION_TOKEN": "from-container"}, "from-session"),
    ],
)
def test_default_token_variables_in_order(launch, secrets, env, accepted):
    created = secrets.create("tokens", SecretString="x")
    port = free_port()
    program = launch(nested_config(port), env=env, token=None, port=port)
    path = "/secretsmanager/get?secretId=" + created["Name"]
    for candidate in ("from-token", "from-session", "from-container"):
        status = program.get(path, {DEFAULT_HEADER: candidate}, token=False)[0]
        assert status == (200 if candidate == accepted else 403), (candidate, status)


def test_custom_headers_and_variables(launch, secrets, sm):
    created = secrets.create("custom", SecretString="x")
    port = free_port()
    config = nested_config(port, '[capabilities.secrets_manager.security]\nssrf_headers = ["X-One", "X-Two"]\nssrf_env_variables = ["TOKEN_B", "TOKEN_A"]\n')
    program = launch(config, env={"TOKEN_A": "alpha", "TOKEN_B": "bravo", "AWS_TOKEN": "aws"}, token=None, port=port)
    path = "/secretsmanager/get?secretId=" + created["Name"]
    expected = sm.get_secret_value(SecretId=created["Name"])
    for headers in ({"X-One": "bravo"}, {"x-two": "bravo"}, {"X-One": "alpha", "X-Two": "bravo"}):
        assert_matches(document(program.get(path, headers, token=False)), expected)
    for headers in ({"X-One": "alpha"}, {"X-Two": "aws"}, {DEFAULT_HEADER: "bravo"}, {"X-Vault-Token": "bravo"}):
        assert_text(program.get(path, headers, token=False), 403, "Bad Token")


def test_token_file_reference(launch, secrets, sm):
    created = secrets.create("filetoken", SecretString="x")
    port = free_port()
    program = launch(
        nested_config(port),
        env={"AWS_TOKEN": "file://{root}/token.txt", "AWS_SESSION_TOKEN": "ignored"},
        token=None,
        port=port,
        files={"token.txt": "\n  file-token-value \t\n"},
    )
    path = "/secretsmanager/get?secretId=" + created["Name"]
    assert_matches(document(program.get(path, {DEFAULT_HEADER: "file-token-value"}, token=False)), sm.get_secret_value(SecretId=created["Name"]))
    for wrong in (f"file://{program.root}/token.txt", "ignored"):
        assert_text(program.get(path, {DEFAULT_HEADER: wrong}, token=False), 403, "Bad Token")


def test_custom_path_prefix(launch, recorder, secrets, sm):
    created = secrets.create("prefixed", SecretString="x")
    name = created["Name"]
    expected = sm.get_secret_value(SecretId=name)
    port = free_port()
    program = launch(nested_config(port, 'path_prefix = "/secrets/"\n'), endpoint=recorder.url, port=port)
    assert_matches(document(program.get("/secrets/" + name)), expected)
    assert_matches(document(program.query(name)), expected)
    assert_text(program.get("/v1/" + name), 404, "Not found")
    assert_text(program.get("/secrets/"), 400, "missing secret ID")

    port = free_port()
    bare = launch(nested_config(port, 'path_prefix = "/v2"\n'), endpoint=recorder.url, port=port)
    assert_matches(document(bare.get("/v2" + name)), expected)
    recorder.take()
    status, body = service_error(lambda: sm.get_secret_value(SecretId="/" + name))
    assert_error(bare.get("/v2/" + name), status, body)
    assert recorder.take() == ["GetSecretValue"]

    port = free_port()
    root = launch(nested_config(port, 'path_prefix = "/"\n'), port=port)
    assert_matches(document(root.get("/" + name)), expected)
    assert_text(root.get("/ping", token=False), 200, "healthy")
    assert_text(root.get("/"), 400, "missing secret ID")
    assert_text(root.get("/secretsmanager/get"), 400, "missing parameter secretId")


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return predicate()


def test_connection_limit(launch, secrets, sm):
    created = secrets.create("limited", SecretString="x")
    port = free_port()
    program = launch(nested_config(port, "max_conn = 2\n"), port=port)
    path = "/secretsmanager/get?secretId=" + created["Name"]
    expected = sm.get_secret_value(SecretId=created["Name"])
    assert_matches(document(program.get(path)), expected)

    idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
    try:
        time.sleep(0.3)
        assert_text(program.get(path), 429, "Connection limit exceeded")
        assert_text(program.get(path, token=False), 429, "Connection limit exceeded")
        # Health checks get two extra connections.
        assert_text(program.get("/ping", token=False), 200, "healthy")
        idle.append(socket.create_connection(("127.0.0.1", port)))
        time.sleep(0.2)
        assert_text(program.get("/ping", token=False), 200, "healthy")
        idle.append(socket.create_connection(("127.0.0.1", port)))
        time.sleep(0.2)
        assert_text(program.get("/ping", token=False), 429, "Connection limit exceeded")
    finally:
        for connection in idle:
            connection.close()
    assert _wait_until(lambda: program.get(path)[0] == 200)
    assert_matches(document(program.get(path)), expected)

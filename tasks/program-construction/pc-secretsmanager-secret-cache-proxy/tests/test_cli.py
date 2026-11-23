"""Invocation, configuration acceptance and startup checks."""

import os
import signal
import socket
import stat
import time

import pytest

from conftest import DEFAULT_HEADER, READY_LINE, assert_matches, assert_same_document, assert_text, document, free_port, nested_config

STS_DENIED = "<ErrorResponse><Error><Type>Sender</Type><Code>InvalidClientTokenId</Code><Message>The security token included in the request is invalid.</Message></Error></ErrorResponse>"
STS_BROKEN = "<ErrorResponse><Error><Type>Receiver</Type><Code>InternalFailure</Code><Message>injected</Message></Error></ErrorResponse>"


def refused(port):
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) != 0


def test_ready_line_and_signals(launch, secrets, sm):
    name = secrets.create("cli", SecretString="x")["Name"]
    for sig in (signal.SIGTERM, signal.SIGINT):
        port = free_port()
        program = launch(nested_config(port), port=port)
        assert [int(p) for p in READY_LINE.findall(program.stdout())] == [port], program.stdout()[-1000:]
        assert_matches(document(program.query(name)), sm.get_secret_value(SecretId=name))
        assert program.stop(sig) == 0
        assert refused(port)


def test_built_in_defaults_without_config(launch, secrets, sm):
    """No --config: port 2773, default header and prefix, and file logging under ./logs."""
    name = secrets.create("defaults", SecretString="x")["Name"]
    program = launch(None, port=2773)
    assert [int(p) for p in READY_LINE.findall(program.stdout())] == [2773]
    expected = sm.get_secret_value(SecretId=name)
    assert_matches(document(program.get("/v1/" + name, {"X-Vault-Token": program.token}, token=False)), expected)
    info = os.lstat(program.root / "logs" / "secrets_manager_provider.log")
    assert stat.S_ISREG(info.st_mode)
    assert program.stop() == 0


def test_short_config_flag_and_capability_only_file(launch, secrets, sm):
    name = secrets.create("short", SecretString="x")["Name"]
    port = free_port()
    program = launch(f"[capabilities.secrets_manager]\nhttp_port = {port}\nvalidate_credentials = false\n", args=["-c", "cfg.toml"], port=port)
    assert_matches(document(program.query(name)), sm.get_secret_value(SecretId=name))
    # Logging defaults to a file in the working directory.
    assert stat.S_ISREG(os.lstat(program.root / "logs" / "secrets_manager_provider.log").st_mode)


INVALID = {
    "unknown-nested-key": ("[capabilities.secrets_manager]\nhttp_port = {port}\nbogus_option = 1\n", "bogus_option"),
    "unknown-top-level-key": ("bogus_option = 1\nhttp_port = {port}\n", "bogus_option"),
    "unknown-cache-key": ("[capabilities.secrets_manager]\nhttp_port = {port}\n[capabilities.secrets_manager.cache]\nttl = 5\n", "ttl"),
    "ttl-too-large": ("[capabilities.secrets_manager]\nhttp_port = {port}\n[capabilities.secrets_manager.cache]\nttl_seconds = 3601\n", "ttl_seconds"),
    "ttl-negative": ("[capabilities.secrets_manager]\nhttp_port = {port}\n[capabilities.secrets_manager.cache]\nttl_seconds = -1\n", None),
    "cache-size-zero": ("[capabilities.secrets_manager]\nhttp_port = {port}\n[capabilities.secrets_manager.cache]\ncache_size = 0\n", "cache_size"),
    "cache-size-too-large": ("[capabilities.secrets_manager]\nhttp_port = {port}\n[capabilities.secrets_manager.cache]\ncache_size = 1001\n", "cache_size"),
    "privileged-port": ("[capabilities.secrets_manager]\nhttp_port = 80\n", "http_port"),
    "port-as-string": ('[capabilities.secrets_manager]\nhttp_port = "{port}"\n', None),
    "max-conn-zero": ("[capabilities.secrets_manager]\nhttp_port = {port}\nmax_conn = 0\n", "max_conn"),
    "max-conn-too-large": ("[capabilities.secrets_manager]\nhttp_port = {port}\nmax_conn = 1001\n", "max_conn"),
    "max-roles-too-large": ("[capabilities.secrets_manager]\nhttp_port = {port}\nmax_roles = 21\n", "max_roles"),
    "prefix-without-slash": ('[capabilities.secrets_manager]\nhttp_port = {port}\npath_prefix = "v1/"\n', "path_prefix"),
    "empty-header-list": ("[capabilities.secrets_manager]\nhttp_port = {port}\n[capabilities.secrets_manager.security]\nssrf_headers = []\n", "ssrf_headers"),
    "empty-variable-list": ("[capabilities.secrets_manager]\nhttp_port = {port}\n[capabilities.secrets_manager.security]\nssrf_env_variables = []\n", "ssrf_env_variables"),
    "buffer-ratio-too-small": ("[capabilities.secrets_manager]\nhttp_port = {port}\n[capabilities.secrets_manager.prefetch]\ncache_buffer_ratio = 0.05\n", "cache_buffer_ratio"),
    "jitter-too-large": ("[capabilities.secrets_manager]\nhttp_port = {port}\n[capabilities.secrets_manager.prefetch]\nmax_jitter_seconds = 11\n", "max_jitter_seconds"),
    "bad-log-level": ('[logging]\nlog_level = "verbose"\n[capabilities.secrets_manager]\nhttp_port = {port}\n', "log_level"),
    "malformed-toml": ("[capabilities.secrets_manager\nhttp_port = {port}\n", None),
    "flat-key-with-logging-table": ("http_port = {port}\nttl_seconds = 5\n[logging]\nlog_to_file = false\n", "http_port"),
    "flat-log-level-with-capabilities": ('log_level = "info"\n[capabilities.secrets_manager]\nhttp_port = {port}\n', "log_level"),
    "flat-port-out-of-range": ("http_port = 70000\nlog_to_file = false\n", "http_port"),
    "flat-port-not-numeric": ('http_port = "abc"\nlog_to_file = false\n', "http_port"),
    "flat-ttl-float": ("http_port = {port}\nttl_seconds = 1.5\nlog_to_file = false\n", "ttl_seconds"),
    "flat-prefetch-ratio": ("http_port = {port}\nlog_to_file = false\n[prefetch]\ncache_buffer_ratio = 2.0\n", "cache_buffer_ratio"),
}


@pytest.mark.parametrize("case", sorted(INVALID))
def test_invalid_configuration_is_rejected(launch, case):
    template, mentioned = INVALID[case]
    port = free_port()
    program = launch(template.replace("{port}", str(port)), port=port, ready=False)
    code = program.wait_exit(10)
    assert code is not None and code != 0, f"{case}: exit status {code}"
    assert refused(port)
    error = program.stderr()
    assert error.strip(), "no diagnostic on stderr"
    if mentioned:
        assert mentioned in error, error[-1500:]
    assert not READY_LINE.search(program.stdout())


def test_missing_config_file(launch):
    program = launch(None, args=["--config", "absent-config.toml"], ready=False)
    assert program.wait_exit(10) not in (None, 0)
    assert "absent-config.toml" in program.stderr()


def test_boundary_values_are_accepted(launch, secrets, sm):
    name = secrets.create("bounds", SecretString="x")["Name"]
    port = free_port()
    body = (
        "max_conn = 1000\nmax_roles = 20\n"
        "[capabilities.secrets_manager.cache]\nttl_seconds = 3600\ncache_size = 1000\n"
        "[capabilities.secrets_manager.prefetch]\ncache_buffer_ratio = 1.0\nmax_jitter_seconds = 0\n"
    )
    program = launch(nested_config(port, body, log_level="WARN"), port=port)
    assert_matches(document(program.query(name)), sm.get_secret_value(SecretId=name))
    port = free_port()
    program = launch(nested_config(port, "[capabilities.secrets_manager.cache]\ncache_size = 1\nttl_seconds = 0\n", log_level="None"), port=port)
    assert_matches(document(program.query(name)), sm.get_secret_value(SecretId=name))


def test_disabled_capability_exits_cleanly(launch):
    port = free_port()
    program = launch(f"[logging]\nlog_to_file = false\n[capabilities.secrets_manager]\nenabled = false\nhttp_port = {port}\n", port=port, ready=False)
    assert program.wait_exit(10) == 0
    assert "not enabled" in program.stderr().lower()
    assert refused(port)


def test_missing_token_source_fails_startup(launch):
    port = free_port()
    program = launch(nested_config(port), token=None, port=port, ready=False)
    assert program.wait_exit(10) not in (None, 0)
    error = program.stderr()
    for variable in ("AWS_TOKEN", "AWS_SESSION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN"):
        assert variable in error, error
    assert refused(port)

    port = free_port()
    custom = nested_config(port, '[capabilities.secrets_manager.security]\nssrf_env_variables = ["APP_PROXY_TOKEN"]\n')
    program = launch(custom, env={"AWS_TOKEN": "unused"}, token=None, port=port, ready=False)
    assert program.wait_exit(10) not in (None, 0)
    assert "APP_PROXY_TOKEN" in program.stderr()

    port = free_port()
    program = launch(nested_config(port), env={"AWS_TOKEN": "file://{root}/no-such-token"}, token=None, port=port, ready=False)
    assert program.wait_exit(10) not in (None, 0)
    assert refused(port)


def test_port_already_in_use(launch):
    with socket.socket() as holder:
        holder.bind(("127.0.0.1", 0))
        holder.listen()
        port = holder.getsockname()[1]
        program = launch(nested_config(port), port=port, ready=False)
        assert program.wait_exit(10) not in (None, 0)
        assert str(port) in program.stderr()


def test_flat_layout(launch, secrets, sm):
    created = secrets.create("flat", SecretString="flat-1")
    name = created["Name"]
    original = sm.get_secret_value(SecretId=name)
    port = free_port()
    config = (
        f'http_port = "{port}"\nttl_seconds = "300"\ncache_size = 10\nlog_to_file = false\nlog_level = "debug"\n'
        'ssrf_headers = ["X-Flat-Token"]\npath_prefix = "/flat/"\nvalidate_credentials = false\n'
        f'[prefetch]\nsecrets = [{{ secret_id = "{name}" }}]\n'
    )
    program = launch(config, port=port)
    time.sleep(1.5)
    sm.put_secret_value(SecretId=name, SecretString="flat-2")
    headers = {"X-Flat-Token": program.token}
    warm = document(program.get("/flat/" + name, headers, token=False))
    assert_matches(warm, original)
    assert_same_document(document(program.get("/secretsmanager/get?secretId=" + name, headers, token=False)), warm)
    assert_text(program.get("/flat/" + name, {DEFAULT_HEADER: program.token}, token=False), 403, "Bad Token")
    assert_text(program.get("/v1/" + name, headers, token=False), 404, "Not found")


def test_startup_credential_check(launch, recorder, secrets, sm):
    name = secrets.create("sts", SecretString="x")["Name"]

    def start(body="", validate=True):
        port = free_port()
        return launch(nested_config(port, body, validate=validate), endpoint=recorder.url, port=port, ready=False)

    program = start()
    assert program.wait_ready()
    assert recorder.take() == ["sts:GetCallerIdentity"]
    assert_matches(document(program.query(name)), sm.get_secret_value(SecretId=name))
    assert recorder.take() == ["GetSecretValue"]

    recorder.rule = lambda op, params: (403, STS_DENIED) if op.startswith("sts:") else None
    program = start()
    assert program.wait_exit(15) not in (None, 0)
    ops = recorder.take()
    assert ops and set(ops) == {"sts:GetCallerIdentity"}
    program = start(validate=False)
    assert program.wait_ready()
    assert recorder.take() == []
    assert_matches(document(program.query(name)), sm.get_secret_value(SecretId=name))
    recorder.take()

    recorder.rule = lambda op, params: (500, STS_BROKEN) if op.startswith("sts:") else None
    program = start()
    assert program.wait_ready(25)
    assert set(recorder.take()) == {"sts:GetCallerIdentity"}
    assert_matches(document(program.query(name)), sm.get_secret_value(SecretId=name))
    program = start("ignore_transient_errors = false\n")
    assert program.wait_exit(25) not in (None, 0)

"""Process lifecycle, configuration handling and the HTTP envelope of the service."""

import signal

import pytest
import requests

from harness import run_program, write_fixture
from lagsvc import CLUSTER, default_sections, free_port, render_config, unique


def assert_envelope(body, url, message, error=False):
    assert type(body["error"]) is bool and body["error"] is error, body
    assert body["message"] == message, body
    assert set(body["request"]) == {"url", "host"}, body
    assert body["request"]["url"] == url, body
    assert type(body["request"]["host"]) is str


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT], ids=["sigterm", "sigint"])
def test_pidfile_and_clean_stop(service, sig):
    svc = service()
    status, text = svc.read_candidate_file("burrow.pid")
    assert status == 0 and text.strip() == str(svc.process.pid)
    admin = svc.get("/burrow/admin")
    assert (admin.status_code, admin.text) == (200, "GOOD")
    ready = svc.get("/burrow/admin/ready")
    assert (ready.status_code, ready.text) == (200, "READY")
    assert svc.stop(sig) == 0
    status, _ = svc.read_candidate_file("burrow.pid")
    assert status != 0, "pid file must be removed on clean shutdown"
    with pytest.raises(requests.ConnectionError):
        requests.get(svc.base + "/burrow/admin", timeout=3)


def test_live_pidfile_refuses_second_instance(kafka, service):
    first = service()
    other = first.root / "second"
    sections = default_sections(kafka, free_port(), first.root)  # same pidfile as the running instance
    write_fixture(other / "burrow.toml", render_config(sections), mode=0o644)
    result = run_program(["--config-dir", other], cwd=first.root, timeout=30)
    assert result.returncode != 0
    assert result.stderr.strip()
    assert first.process.poll() is None
    assert first.json("/v3/kafka")["clusters"] == [CLUSTER]
    status, text = first.read_candidate_file("burrow.pid")
    assert status == 0 and text.strip() == str(first.process.pid)


def test_configuration_failures(scratch):
    empty = scratch / "empty"
    write_fixture(empty / "placeholder", "")
    missing = run_program(["--config-dir", empty], cwd=scratch)
    assert missing.returncode != 0 and missing.stderr.strip()

    broken = scratch / "broken"
    write_fixture(broken / "burrow.toml", "this is = = not toml\n")
    invalid = run_program(["--config-dir", broken], cwd=scratch)
    assert invalid.returncode != 0 and invalid.stderr.strip()

    flag = run_program(["--no-such-flag"], cwd=scratch)
    assert flag.returncode != 0 and flag.stderr.strip()


def test_invalid_group_pattern_fails_startup(service):
    svc = service(wait=False, storage={"group-allowlist": "(["})
    assert svc.process.wait(30) != 0


def test_cluster_routes_and_errors(kafka, service):
    svc = service()
    body = svc.json("/v3/kafka")
    assert set(body) == {"error", "message", "clusters", "request"}
    assert_envelope(body, "/v3/kafka", "cluster list returned")
    assert body["clusters"] == [CLUSTER]

    for path in (f"/v3/kafka/{CLUSTER}", f"/v3/config/cluster/{CLUSTER}"):
        detail = svc.json(path)
        assert set(detail) == {"error", "message", "module", "request"}
        assert_envelope(detail, path, "cluster module detail returned")
        assert detail["module"] == {
            "class-name": "kafka",
            "servers": kafka.servers,
            "client-profile": {"name": "lag", "client-id": "lag-checker", "kafka-version": "2.8.0", "tls": None, "sasl": None},
            "topic-refresh": 1,
            "offset-refresh": 1,
        }

    errors = {
        "/v3/kafka/nowhere": "cluster module not found",
        "/v3/kafka/nowhere/topic": "cluster not found",
        "/v3/kafka/nowhere/consumer": "cluster not found",
        f"/v3/kafka/{CLUSTER}/topic/no-such-topic": "cluster or topic not found",
        f"/v3/kafka/{CLUSTER}/consumer/no-such-group": "cluster or consumer not found",
        "/v3/config/cluster/nowhere": "cluster module not found",
    }
    for path, message in errors.items():
        body = svc.json(path, status=404)
        assert set(body) == {"error", "message", "request"}, body
        assert_envelope(body, path, message, error=True)

    unrouted = svc.get("/v3/unknown/route")
    assert unrouted.status_code == 404
    assert unrouted.json() == {"error": True, "message": "invalid request type", "result": {}}
    assert svc.request("POST", "/v3/kafka").status_code == 405


def test_unknown_group_evaluation_is_notfound(service):
    svc = service()
    group = unique("absent")
    for kind in ("status", "lag"):
        path = f"/v3/kafka/{CLUSTER}/consumer/{group}/{kind}"
        body = svc.json(path, status=404)
        assert set(body) == {"error", "message", "status", "request"}
        assert_envelope(body, path, "consumer status returned", error=False)
        status = body["status"]
        assert status == {
            "cluster": CLUSTER,
            "group": group,
            "status": "NOTFOUND",
            "complete": status["complete"],
            "partitions": [],
            "partition_count": 0,
            "maxlag": None,
            "totallag": 0,
        }
        assert type(status["complete"]) in (int, float) and status["complete"] == 1


def test_cors_header(service):
    plain = service()
    assert "Access-Control-Allow-Origin" not in plain.get("/v3/kafka").headers
    cors = service(general={"access-control-allow-origin": "https://dash.example"})
    for path in ("/v3/kafka", "/burrow/admin", f"/v3/kafka/{CLUSTER}/consumer/missing"):
        assert cors.get(path).headers.get("Access-Control-Allow-Origin") == "https://dash.example"


def test_configuration_endpoints(kafka, service):
    svc = service(
        general={"access-control-allow-origin": "*"},
        storage={"intervals": 6, "min-distance": 2, "expire-group": 3600, "group-allowlist": "^keep-"},
        evaluator={"expire-cache": 4},
        consumer={"start-latest": False, "group-denylist": "^skip-"},
    )
    main = svc.json("/v3/config")
    assert set(main) == {"error", "message", "request", "general", "logging", "zookeeper", "httpserver"}
    assert_envelope(main, "/v3/config", "main config returned")
    assert main["general"] == {"pidfile": str(svc.root / "burrow.pid"), "stdout-logfile": "", "access-control-allow-origin": "*"}
    assert main["logging"] == {
        "filename": "",
        "max-size": 100,
        "max-backups": 10,
        "max-age": 30,
        "use-local-time": False,
        "use-compression": False,
        "level": "info",
    }
    assert type(main["logging"]["use-local-time"]) is bool and type(main["logging"]["use-compression"]) is bool
    assert main["zookeeper"] == {"servers": None, "timeout": 0, "root-path": ""}
    assert main["httpserver"] == {"default": {"address": f"127.0.0.1:{svc.port}", "tls": "", "timeout": 300}}

    expected_lists = {"storage": ["default"], "evaluator": ["default"], "cluster": [CLUSTER], "consumer": [CLUSTER], "notifier": []}
    for coordinator, modules in expected_lists.items():
        path = f"/v3/config/{coordinator}"
        body = svc.json(path)
        assert set(body) == {"error", "message", "request", "coordinator", "modules"}
        assert_envelope(body, path, "module list returned")
        assert body["coordinator"] == coordinator and body["modules"] == modules

    storage = svc.json("/v3/config/storage/default")
    assert_envelope(storage, "/v3/config/storage/default", "storage module detail returned")
    assert storage["module"] == {"class-name": "inmemory", "intervals": 6, "min-distance": 2, "group-allowlist": "^keep-", "expire-group": 3600}

    evaluator = svc.json("/v3/config/evaluator/default")
    assert_envelope(evaluator, "/v3/config/evaluator/default", "evaluator module detail returned")
    assert evaluator["module"] == {"class-name": "caching", "expire-cache": 4}

    consumer = svc.json(f"/v3/config/consumer/{CLUSTER}")
    assert_envelope(consumer, f"/v3/config/consumer/{CLUSTER}", "consumer module detail returned")
    assert consumer["module"] == {
        "class-name": "kafka",
        "cluster": CLUSTER,
        "servers": kafka.servers,
        "group-allowlist": "",
        "zookeeper-path": "",
        "zookeeper-timeout": 0,
        "client-profile": {"name": "lag", "client-id": "lag-checker", "kafka-version": "2.8.0", "tls": None, "sasl": None},
        "offsets-topic": "__consumer_offsets",
        "start-latest": False,
    }
    assert type(consumer["module"]["start-latest"]) is bool

    for path, message in {
        "/v3/config/storage/other": "storage module not found",
        "/v3/config/evaluator/other": "evaluator module not found",
        "/v3/config/consumer/other": "consumer module not found",
    }.items():
        body = svc.json(path, status=404)
        assert_envelope(body, path, message, error=True)


def test_log_level_admin(service):
    svc = service()
    level = svc.json("/v3/admin/loglevel")
    assert set(level) == {"error", "message", "level", "request"}
    assert_envelope(level, "/v3/admin/loglevel", "log level returned")
    assert level["level"] == "info"

    changed = svc.request("POST", "/v3/admin/loglevel", data='{"level": "warn"}')
    assert changed.status_code == 200
    assert_envelope(changed.json(), "/v3/admin/loglevel", "set log level")
    assert svc.json("/v3/admin/loglevel")["level"] == "warn"

    unknown = svc.request("POST", "/v3/admin/loglevel", data='{"level": "verbose"}')
    assert unknown.status_code == 404
    assert_envelope(unknown.json(), "/v3/admin/loglevel", "unknown log level", error=True)
    garbage = svc.request("POST", "/v3/admin/loglevel", data="not json")
    assert garbage.status_code == 400
    assert_envelope(garbage.json(), "/v3/admin/loglevel", "could not decode message body", error=True)
    assert svc.json("/v3/admin/loglevel")["level"] == "warn"

    assert svc.request("POST", "/v3/admin/loglevel", data='{"level": "debug"}').status_code == 200
    assert svc.json("/v3/admin/loglevel")["level"] == "debug"


def test_environment_overrides_configuration(kafka, service, topics):
    topic = topics(1)
    kafka.produce(topic, {0: 4})
    svc = service(env={"BURROW_STORAGE_DEFAULT_INTERVALS": "7"})
    assert svc.json("/v3/config/storage/default")["module"]["intervals"] == 7
    svc.wait_topic(topic, [4])
    group = unique("envgrp")
    kafka.commit(group, topic, {0: 1})
    svc.wait_window(group, topic, 0, [1])
    slots = svc.detail(group)["topics"][topic][0]["offsets"]
    assert len(slots) == 7 and slots[:6] == [None] * 6

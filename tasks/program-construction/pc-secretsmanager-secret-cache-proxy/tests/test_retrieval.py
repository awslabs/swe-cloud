"""Single lookups: response documents and service errors compared with Secrets Manager itself."""

import base64
import json
from urllib.parse import quote

from conftest import assert_error, assert_matches, document, free_port, nested_config, service_error


def test_lookup_by_name_matches_service(agent, recorder, secrets, sm):
    # Adapted from integration-tests/tests/secret_retrieval.rs::test_secret_retrieval_by_name.
    created = secrets.create("basic", SecretString='{"username":"app","password":"p@ss"}')
    doc = document(agent.query(created["Name"]))
    assert_matches(doc, sm.get_secret_value(SecretId=created["Name"]))
    assert doc["VersionId"] == created["VersionId"] and doc["VersionStages"] == ["AWSCURRENT"]
    assert recorder.take() == ["GetSecretValue"]


def test_lookup_by_arn_and_path_form(agent, recorder, secrets, sm):
    # secret_retrieval.rs::test_secret_retrieval_by_arn and configuration.rs::test_path_based_requests.
    created = secrets.create("forms", SecretString="by-arn")
    expected = sm.get_secret_value(SecretId=created["ARN"])
    assert_matches(document(agent.query(created["ARN"])), expected)
    assert_matches(document(agent.get("/v1/" + secrets.name("forms"))), expected)
    assert recorder.take() == ["GetSecretValue", "GetSecretValue"]


def test_binary_secret_is_base64(agent, secrets, sm):
    # secret_retrieval.rs::test_binary_secret_retrieval.
    payload = bytes(range(256)) + b"\x00\xff tail"
    created = secrets.create("binary", SecretBinary=payload)
    doc = document(agent.query(created["Name"]))
    assert "SecretString" not in doc
    assert base64.b64decode(doc["SecretBinary"], validate=True) == payload
    assert_matches(doc, sm.get_secret_value(SecretId=created["Name"]))


def test_large_secret(agent, secrets, sm):
    # secret_retrieval.rs::test_large_secret_retrieval (64 KiB, the service maximum).
    value = json.dumps({"blob": "x" * (65536 - 20)})[:65536]
    created = secrets.create("large", SecretString=value)
    doc = document(agent.query(created["Name"]))
    assert doc["SecretString"] == value
    assert_matches(doc, sm.get_secret_value(SecretId=created["Name"]))


def test_version_stage_and_version_id_lookups(agent, secrets, sm):
    # secret_retrieval.rs::test_version_stage_retrieval / test_version_id_retrieval.
    created = secrets.create("versions", SecretString="one")
    name, v1 = created["Name"], created["VersionId"]
    v2 = sm.put_secret_value(SecretId=name, SecretString="two")["VersionId"]
    v3 = sm.put_secret_value(SecretId=name, SecretString="three", VersionStages=["AWSPENDING"])["VersionId"]
    sm.update_secret_version_stage(SecretId=name, VersionStage="blue", MoveToVersionId=v3)

    cases = [
        ({"versionStage": "AWSPREVIOUS"}, {"VersionStage": "AWSPREVIOUS"}, v1),
        ({"versionStage": "AWSPENDING"}, {"VersionStage": "AWSPENDING"}, v3),
        ({"versionStage": "blue"}, {"VersionStage": "blue"}, v3),
        ({"versionId": v1}, {"VersionId": v1}, v1),
        ({"versionId": v2, "versionStage": "AWSCURRENT"}, {"VersionId": v2, "VersionStage": "AWSCURRENT"}, v2),
        ({}, {}, v2),
    ]
    for params, service_params, version in cases:
        doc = document(agent.query(name, **params))
        assert doc["VersionId"] == version, params
        assert_matches(doc, sm.get_secret_value(SecretId=name, **service_params))
    assert sorted(document(agent.query(name, versionStage="blue"))["VersionStages"]) == ["AWSPENDING", "blue"]

    # The path form accepts the same version parameters.
    doc = document(agent.get(f"/v1/{name}?versionId={v1}"))
    assert doc["VersionId"] == v1 and doc["SecretString"] == "one"
    doc = document(agent.get(f"/v1/{name}?versionStage=AWSPENDING"))
    assert doc["VersionId"] == v3 and doc["SecretString"] == "three"


def test_service_errors_pass_through(agent, recorder, secrets, sm):
    # secret_retrieval.rs::test_real_nonexistent_secret, extended to version errors.
    created = secrets.create("errors", SecretString="one")
    name, v1 = created["Name"], created["VersionId"]
    sm.put_secret_value(SecretId=name, SecretString="two")
    missing = secrets.name("never-created")
    absent_version = "00000000-0000-4000-8000-000000000000"
    cases = [
        ({"secret_id": missing}, {"SecretId": missing}),
        ({"secret_id": name, "versionId": absent_version}, {"SecretId": name, "VersionId": absent_version}),
        ({"secret_id": name, "versionStage": "NO_SUCH_LABEL"}, {"SecretId": name, "VersionStage": "NO_SUCH_LABEL"}),
        ({"secret_id": name, "versionId": v1, "versionStage": "AWSCURRENT"}, {"SecretId": name, "VersionId": v1, "VersionStage": "AWSCURRENT"}),
    ]
    for params, service_params in cases:
        status, body = service_error(lambda: sm.get_secret_value(**service_params))
        assert 400 <= status < 500
        secret_id = params.pop("secret_id")
        recorder.take()
        assert_error(agent.query(secret_id, **params), status, body)
        ops = recorder.take()
        assert ops and set(ops) == {"GetSecretValue"}, service_params
    status, body = service_error(lambda: sm.get_secret_value(SecretId=missing))
    assert_error(agent.get("/v1/" + missing), status, body)


def test_errors_are_not_cached(agent, recorder, secrets, sm):
    name = secrets.name("late")
    status, body = service_error(lambda: sm.get_secret_value(SecretId=name))
    for _ in range(2):
        assert_error(agent.query(name), status, body)
        ops = recorder.take()
        assert ops and set(ops) == {"GetSecretValue"}
    secrets.create("late", SecretString="arrived")
    doc = document(agent.query(name))
    assert doc["SecretString"] == "arrived"
    assert_matches(doc, sm.get_secret_value(SecretId=name))


def test_secret_marked_for_deletion(agent, secrets, sm):
    created = secrets.create("doomed", SecretString="bye")
    sm.delete_secret(SecretId=created["Name"], RecoveryWindowInDays=7)
    status, body = service_error(lambda: sm.get_secret_value(SecretId=created["Name"]))
    assert_error(agent.query(created["Name"]), status, body)


def test_path_ids_are_used_as_written(agent, recorder, secrets, sm):
    """Query values are URL-decoded; the path form passes the raw (still percent-encoded) path."""
    slashed = secrets.create("team/app/db", SecretString="slashes")
    plus = secrets.create("x+y@z", SecretString="plus")
    expected_slash = sm.get_secret_value(SecretId=slashed["Name"])
    expected_plus = sm.get_secret_value(SecretId=plus["Name"])

    assert_matches(document(agent.get("/v1/" + slashed["Name"])), expected_slash)
    assert_matches(document(agent.get("/secretsmanager/get?secretId=" + quote(slashed["Name"], safe=""))), expected_slash)
    assert_matches(document(agent.get("/v1/" + plus["Name"])), expected_plus)
    assert_matches(document(agent.get("/secretsmanager/get?secretId=" + quote(plus["Name"], safe=""))), expected_plus)
    recorder.take()

    # Percent-encoding in the path is not decoded, so these ids name no secret.
    for path, literal in [
        ("/v1/" + slashed["Name"].replace("/", "%2F"), slashed["Name"].replace("/", "%2F")),
        ("/v1/" + plus["Name"].replace("+", "%2B"), plus["Name"].replace("+", "%2B")),
    ]:
        status, body = service_error(lambda: sm.get_secret_value(SecretId=literal))
        assert_error(agent.get(path), status, body)
        ops = recorder.take()
        assert ops and set(ops) == {"GetSecretValue"}
    # In the query string a raw '+' decodes to a space.
    spaced = plus["Name"].replace("+", " ")
    status, body = service_error(lambda: sm.get_secret_value(SecretId=spaced))
    assert_error(agent.get("/secretsmanager/get?secretId=" + plus["Name"].replace("@", "%40")), status, body)
    ops = recorder.take()
    assert ops and set(ops) == {"GetSecretValue"}


def test_last_repeated_query_parameter_wins(agent, secrets, sm):
    first = secrets.create("first", SecretString="first-value")
    second = secrets.create("second", SecretString="second-value")
    doc = document(agent.get(f"/secretsmanager/get?secretId={first['Name']}&secretId={second['Name']}"))
    assert_matches(doc, sm.get_secret_value(SecretId=second["Name"]))


def test_region_setting_selects_service_region(launch, secrets):
    east = secrets.create("regional", SecretString="east")
    west = secrets.create("regional-west", region="us-west-2", SecretString="west")
    port = free_port()
    program = launch(nested_config(port, 'region = "us-west-2"\n'), port=port)
    west_client = secrets.client("us-west-2")
    assert_matches(document(program.query(west["Name"])), west_client.get_secret_value(SecretId=west["Name"]))
    status, body = service_error(lambda: west_client.get_secret_value(SecretId=east["Name"]))
    assert_error(program.query(east["Name"]), status, body)

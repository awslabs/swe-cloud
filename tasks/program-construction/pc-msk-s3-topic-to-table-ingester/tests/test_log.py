"""Delta log maintenance and sharing: checkpoints, checkpoint-only logs, commits by another writer."""

import io
import json
import time

import boto3
import pyarrow.parquet as pq
from lake import DLQ_FIELDS, assert_rows_by, json_value, quiet, txn_actions

CHECKPOINT = "_delta_log/00000000000000000010.checkpoint.parquet"


def checkpoint_files(table):
    return sorted(k for k in table.keys() if "checkpoint" in k)


def read_checkpoint(table):
    rows = pq.read_table(io.BytesIO(table.read(CHECKPOINT))).to_pylist()
    actions = []
    for row in rows:
        present = [k for k, v in row.items() if v is not None]
        assert len(present) == 1, f"checkpoint row with {present}"
        actions.append((present[0], row[present[0]]))
    return actions


def check_checkpoint(table, txns):
    assert checkpoint_files(table) == [CHECKPOINT, "_delta_log/_last_checkpoint"]
    last = json.loads(table.read("_delta_log/_last_checkpoint"))
    assert last["version"] == 10
    actions = read_checkpoint(table)
    assert last["size"] == len(actions)
    by_kind = {}
    for kind, value in actions:
        by_kind.setdefault(kind, []).append(value)
    assert sorted(by_kind) == sorted(["protocol", "metaData", "add"] + (["txn"] if txns else []))
    assert [(p["minReaderVersion"], p["minWriterVersion"]) for p in by_kind["protocol"]] == [(1, 2)]
    (meta,) = by_kind["metaData"]
    assert json_value(meta["schemaString"]) == json_value(table.metadata()["schemaString"])
    assert list(meta["partitionColumns"]) == []
    expected_paths = sorted(a["path"] for v in range(1, 11) for a in table.adds(v))
    assert sorted(a["path"] for a in by_kind["add"]) == expected_paths
    assert {t["appId"]: t["version"] for t in by_kind.get("txn", [])} == txns


def test_checkpoint_written_at_tenth_version_of_both_tables(lake):
    """Adapted from the --checkpoints option and src/delta_helpers.rs try_create_checkpoint."""
    topic = lake.topic()
    table = lake.table([("id", "integer")])
    dlq = lake.table(DLQ_FIELDS)
    messages = []
    for i in range(1, 12):
        messages.append((0, {"id": i}))
        if i <= 10:
            messages.append((0, f"bad-{i}".encode()))
    lake.produce(topic, messages)
    lake.start(lake.args(topic, table, "-c", "-m", 1, "-b", 1, "--dlq_table_location", dlq.uri, app="ck", latency=30))
    table.wait_version(11, timeout=45)
    quiet(table, 11, 2)
    assert dlq.versions() == list(range(11))
    assert [txn_actions(table.commit(v)) for v in range(1, 12)] == [{("ck-0", 2 * (v - 1)): 1} for v in range(1, 12)]
    check_checkpoint(table, {"ck-0": 18})
    check_checkpoint(dlq, {})
    assert sorted(r["id"] for r in table.rows()) == list(range(1, 12))


def test_no_checkpoint_without_the_option(lake):
    topic = lake.topic()
    table = lake.table([("id", "integer")])
    lake.produce(topic, [(0, {"id": i}) for i in range(10)])
    lake.start(lake.args(topic, table, "-m", 1, "-b", 1, app="nock", latency=30))
    table.wait_version(10, timeout=40)
    quiet(table, 10, 2)
    assert checkpoint_files(table) == []
    assert table.txns() == {"nock-0": 9}


def test_resume_from_a_log_that_starts_at_a_checkpoint(lake):
    """Once earlier commit files are gone, the stored offset must be read from the checkpoint."""
    topic = lake.topic()
    table = lake.table([("id", "integer"), ("off", "long")])
    lake.produce(topic, [(0, {"id": i}) for i in range(10)])
    first = lake.start(lake.args(topic, table, "-t", "off: kafka.offset", "-c", "-m", 1, "-b", 1, app="cp", latency=30))
    table.wait_version(10, timeout=40)
    quiet(table, 10, 2)
    first.stop()
    assert CHECKPOINT in table.keys()
    s3 = boto3.client("s3")
    for v in range(10):
        s3.delete_object(Bucket=table.bucket, Key=f"{table.prefix}/_delta_log/{v:020d}.json")
    lake.produce(topic, [(0, {"id": 10}), (0, {"id": 11})])
    second = lake.start(lake.args(topic, table, "-t", "off: kafka.offset", app="cp"))
    table.wait_version(11)
    quiet(table, 11, 3)
    second.stop()
    assert table.versions() == [10, 11]
    assert txn_actions(table.commit(11)) == {("cp-0", 11): 1}
    assert_rows_by(table.rows([11]), [{"id": 10, "off": 10}, {"id": 11, "off": 11}], key="id")


def test_commit_by_another_writer_with_schema_change(lake):
    """Adapted from tests/schema_update_tests.rs: a concurrent metaData change is picked up before the next commit."""
    topic = lake.topic()
    table = lake.table([("id", "integer")])
    lake.produce(topic, [(0, {"id": 1})])
    lake.start(lake.args(topic, table, "-m", 1, "-b", 1, app="evolve", latency=30))
    table.wait_version(1)
    schema = {"type": "struct", "fields": [{"name": "id", "type": "integer", "nullable": True, "metadata": {}},
                                           {"name": "extra", "type": "string", "nullable": True, "metadata": {}}]}
    external = (json.dumps({"txn": {"appId": "other-0", "version": 99, "lastUpdated": 1}}) + "\n"
                + json.dumps({"metaData": {"id": "b9d3f8a0-0f4e-4c56-9a43-0d8a0ac2d1f7", "format": {"provider": "parquet", "options": {}},
                                           "schemaString": json.dumps(schema), "partitionColumns": [], "createdTime": 1700000000000,
                                           "configuration": {}}}) + "\n")
    table.put_log(2, external)
    boto3.client("dynamodb").put_item(TableName=lake.lock_table, Item={
        "tablePath": {"S": table.uri}, "fileName": {"S": "00000000000000000002.json"},
        "tempPath": {"S": "_commit_external.json.tmp"}, "complete": {"S": "true"},
        "expireTime": {"N": str(int(time.time()) + 86400)}})
    lake.produce(topic, [(0, {"id": 2, "extra": "e2"})])
    table.wait_version(3)
    lake.produce(topic, [(0, {"id": 3, "extra": "e3"})])
    table.wait_version(4)
    quiet(table, 4, 2)
    assert table.read("_delta_log/00000000000000000002.json").decode() == external
    assert [txn_actions(table.commit(v)) for v in (3, 4)] == [{("evolve-0", 1): 1}, {("evolve-0", 2): 1}]
    assert table.txns() == {"evolve-0": 2, "other-0": 99}
    assert_rows_by(table.rows([1]), [{"id": 1, "extra": None}], key="id")
    assert_rows_by(table.rows([3, 4]), [{"id": 2, "extra": "e2"}, {"id": 3, "extra": "e3"}], key="id")
    names = sorted(i["fileName"]["S"] for i in lake.lock_items(table))
    assert names == [f"{v:020d}.json" for v in (1, 2, 3, 4)]
    assert all(i["complete"] == {"S": "true"} for i in lake.lock_items(table))

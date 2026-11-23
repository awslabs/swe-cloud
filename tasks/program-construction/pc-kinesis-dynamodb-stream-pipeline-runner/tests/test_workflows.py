"""Multi-step interactions: two live instances sharing a table, and a stream-to-stream bridge."""

import time

from conftest import as_multiset, connection, consumer_config, final_row, owner, read_records, scan, wait_for


def test_second_instance_takes_half_and_returns_them_on_shutdown(lab):
    stream, shards = lab.stream(4)
    table = lab.table()
    first = [lab.put(stream, f"a{i}", "pk", i, 4, s) for i, s in enumerate(shards)]
    lab.write_config("a.yaml", consumer_config([stream], table, out="a.jsonl"))
    lab.write_config("b.yaml", consumer_config([stream], table, out="b.jsonl"))
    a = lab.start("a.yaml")
    wait_for(lambda: len(lab.lines("a.jsonl")) >= 4, 30, "first instance messages")
    rows = wait_for(lambda: (r := scan(table)) and len(r) == 4 and all("SequenceNumber" in x for x in r.values()) and r, 15, "four checkpoints")
    client_a = owner(next(iter(rows.values())))
    assert all(owner(r) == client_a for r in rows.values())

    b = lab.start("b.yaml")

    def split():
        owners = [owner(r) for r in scan(table).values()]
        others = {o for o in owners if o != client_a}
        return len(others) == 1 and owners.count(client_a) == 2 and owners

    wait_for(split, 25, "a two/two split")
    time.sleep(3)
    rows = scan(table)
    assert split(), rows
    client_b = next(owner(r) for r in rows.values() if owner(r) != client_a)
    mine = {client_a: "a.jsonl", client_b: "b.jsonl"}
    second = {s: lab.put(stream, f"b{i}", "pk", i, 4, s) for i, s in enumerate(shards)}
    wait_for(lambda: len(lab.lines("a.jsonl")) + len(lab.lines("b.jsonl")) >= 8, 20, "second wave")
    time.sleep(2)
    for shard, record in second.items():
        holder = owner(rows[(stream, shard)])
        assert record in lab.lines(mine[holder]), (shard, holder)

    assert b.stop() == 0
    wait_for(lambda: all(owner(r) == client_a for r in scan(table).values()), 10, "released shards claimed back")
    third = [lab.put(stream, f"c{i}", "pk", i, 4, s) for i, s in enumerate(shards)]
    wait_for(lambda: all(r in lab.lines("a.jsonl") for r in third), 20, "third wave on the remaining instance")
    time.sleep(1.5)
    assert a.stop() == 0
    combined = lab.lines("a.jsonl") + lab.lines("b.jsonl")
    assert as_multiset(combined) == as_multiset(first + list(second.values()) + third)
    assert scan(table) == {(stream, r["shard"]): final_row(stream, r["shard"], r["sequence"]) for r in third}


def test_bridge_copies_records_and_resumes_without_duplicates(lab):
    source, shards = lab.stream(2, "src")
    target, (target_shard,) = lab.stream(1, "dst")
    table = lab.table()
    config = consumer_config(
        [source],
        table,
        mapping=None,
        output={
            "aws_kinesis": {
                "stream": target,
                "partition_key": '${! meta("kinesis_partition_key") }',
                **connection(),
                "batching": {"count": 2, "period": "300ms"},
            }
        },
    )
    lab.write_config("bridge.yaml", config)
    first = [lab.put(source, f"payload-{i}", f"key-{i}", i % 2, 2) for i in range(5)]
    bridge = lab.start("bridge.yaml")
    wait_for(lambda: len(read_records(target)) >= 5, 30, "copied records")
    newest = {}
    for record in first:
        newest[(source, record["shard"])] = record["sequence"]
    wait_for(lambda: all((scan(table) or {}).get(k, {}).get("SequenceNumber") == ("S", v) for k, v in newest.items()), 15, "source checkpoints")
    assert bridge.stop() == 0
    second = [lab.put(source, f"payload-{i}", f"key-{i}", i % 2, 2) for i in range(5, 8)]
    for record in second:
        newest[(source, record["shard"])] = record["sequence"]
    again = lab.start("bridge.yaml")
    wait_for(lambda: len(read_records(target)) >= 8, 30, "records copied after restart")
    time.sleep(2.5)
    assert again.stop() == 0
    copied = read_records(target)
    assert sorted(copied) == sorted((target_shard, r["partition_key"], r["data"].encode()) for r in first + second)
    assert scan(table) == {key: final_row(*key, seq) for key, seq in newest.items()}

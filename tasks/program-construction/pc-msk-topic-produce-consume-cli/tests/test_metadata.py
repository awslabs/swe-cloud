"""Metadata listing in text and JSON form, checked against an independent admin client."""

import json
import re

from support import same_json


def broker_facts(kafka):
    md = kafka.metadata()
    return md, sorted(md.brokers.values(), key=lambda b: b.id)


def test_text_listing_for_one_topic(kafka):
    topic = kafka.topic(3)
    result = kafka.run("-L", "-t", topic)
    assert result.returncode == 0, result.stderr
    md, brokers = broker_facts(kafka)
    text = result.stdout.decode()
    header, rest = text.split("\n", 1)
    assert re.fullmatch(rf"Metadata for {re.escape(topic)} \(from broker -?\d+: \S+\):", header), header
    expected = f" {len(brokers)} brokers:\n"
    for b in brokers:
        expected += f"  broker {b.id} at {b.host}:{b.port}{' (controller)' if b.id == md.controller_id else ''}\n"
    expected += f" 1 topics:\n  topic \"{topic}\" with 3 partitions:\n"
    for p in sorted(md.topics[topic].partitions.values(), key=lambda p: p.id):
        expected += f"    partition {p.id}, leader {p.leader}, replicas: {','.join(map(str, p.replicas))}, isrs: {','.join(map(str, p.isrs))}\n"
    assert rest == expected


def test_json_listing_for_one_topic(kafka):
    topic = kafka.topic(2)
    result = kafka.run("-L", "-J", "-t", topic)
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    md, brokers = broker_facts(kafka)
    origin = doc.get("originating_broker")
    assert isinstance(origin, dict) and set(origin) == {"id", "name"}
    assert type(origin["id"]) in (int, float) and origin["id"] == int(origin["id"]) and type(origin["name"]) is str
    partitions = [
        {"partition": p.id, "leader": p.leader, "replicas": [{"id": r} for r in p.replicas], "isrs": [{"id": i} for i in p.isrs]}
        for p in sorted(md.topics[topic].partitions.values(), key=lambda p: p.id)
    ]
    same_json(doc, {
        "originating_broker": origin,
        "query": {"topic": topic},
        "controllerid": md.controller_id,
        "brokers": [{"id": b.id, "name": f"{b.host}:{b.port}"} for b in brokers],
        "topics": [{"topic": topic, "partitions": partitions}],
    })


def test_listing_all_topics(kafka):
    mine = {kafka.topic(1): 1, kafka.topic(4): 4}
    result = kafka.run("-L", "-J")
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    same_json(doc["query"], {"topic": "*"})
    listed = {t["topic"]: t for t in doc["topics"]}
    assert len(listed) == len(doc["topics"])
    for topic, count in mine.items():
        assert [p["partition"] for p in listed[topic]["partitions"]] == list(range(count))
    text = kafka.run("-L")
    assert text.returncode == 0, text.stderr
    body = text.stdout.decode()
    assert body.startswith("Metadata for all topics (from broker ")
    topics_line = re.search(r"^ (\d+) topics:$", body, re.M)
    assert topics_line and int(topics_line.group(1)) == len(re.findall(r'^  topic "', body, re.M))
    for topic, count in mine.items():
        assert f'  topic "{topic}" with {count} partitions:\n' in body

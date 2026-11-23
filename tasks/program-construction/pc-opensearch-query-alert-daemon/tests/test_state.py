"""State documents, schedules and resuming from recorded state after a restart."""

import datetime as dt
import time

from alertkit import (
    Daemon,
    check_state_doc,
    cleaned,
    file_rule,
    instant,
    now,
    output_dir,
    read_lines,
    rfc3339,
    run_once,
    tag,
    wait_lines,
    wait_state,
)

INFO = {"query": {"term": {"level": "info"}}, "_source": ["msg"]}
HEALTHY = [{"msg": "healthy"}]


def test_state_document_fields_and_rule_name(cluster, root, logs, daemon):
    out = output_dir(root)
    suffix = tag()
    name = f"Disk  Usage_HIGH (Prod) {suffix}"
    daemon.add_rule("rule.json", file_rule(name, logs, INFO, out, "alerts.jsonl"))
    started = now().replace(microsecond=0)
    run_once(daemon, cluster, [f"disk--usage_high-(prod)-{suffix}"])
    finished = now()

    (hit,) = cluster.state_hits(f"disk--usage_high-(prod)-{suffix}")
    stamp, next_query = check_state_doc(hit["_source"], f"disk--usage_high-(prod)-{suffix}", HEALTHY)
    assert started <= stamp <= finished
    assert hit["_index"] == "go-es-alerts-status-0.0.3-" + stamp.strftime("%Y.%m.%d")
    # "@every 1h" counts from the execution's whole second.
    assert 3599 <= (next_query - stamp).total_seconds() <= 3600
    assert next_query.microsecond == 0
    # The original name is kept in the alert itself.
    assert read_lines(out / "alerts.jsonl")[0]["rule_name"] == name


def test_every_schedule_repeats(cluster, root, logs, daemon):
    out = output_dir(root)
    name = "Every Two " + tag()
    daemon.add_rule("rule.json", file_rule(name, logs, INFO, out, "alerts.jsonl", schedule="@every 2s"))
    daemon.start()
    docs = wait_state(cluster, cleaned(name), 3, timeout=20)
    lines = wait_lines(out / "alerts.jsonl", 3, timeout=5)
    assert daemon.stop() == 0
    assert len(docs) >= 3 and len(lines) >= 3, (docs, lines)

    nexts = [check_state_doc(doc, cleaned(name), HEALTHY)[1] for doc in docs[:3]]
    assert all(moment.microsecond == 0 for moment in nexts)
    assert [(b - a).total_seconds() for a, b in zip(nexts, nexts[1:])] == [2.0, 2.0]
    received = [instant(line["received_at"]) for line in lines[:3]]
    # Each later execution happens at the next_query recorded by the previous one.
    for previous_next, executed in zip(nexts, received[1:]):
        assert previous_next <= executed < previous_next + dt.timedelta(seconds=1.5)


def test_cron_expressions_with_seconds(cluster, root, logs, daemon):
    out = output_dir(root)
    suffix = tag()
    rules = {
        "six": file_rule("Cron Six " + suffix, logs, INFO, out, "six.jsonl", schedule="*/3 * * * * *"),
        "four": file_rule("Cron Four " + suffix, logs, INFO, out, "four.jsonl", schedule="*/4 * * * * *"),
    }
    for key, rule in rules.items():
        daemon.add_rule(f"{key}.json", rule)
    daemon.start()
    docs = {key: wait_state(cluster, cleaned(rule["name"]), 2, timeout=20) for key, rule in rules.items()}
    assert daemon.stop() == 0
    for key, step in (("six", 3), ("four", 4)):
        nexts = [check_state_doc(doc, cleaned(rules[key]["name"]), HEALTHY)[1] for doc in docs[key][:2]]
        assert len(nexts) == 2, docs[key]
        # Both six-field expressions use the leading seconds field.
        assert all(moment.second % step == 0 and moment.microsecond == 0 for moment in nexts), nexts
        assert (nexts[1] - nexts[0]).total_seconds() == step
        lines = read_lines(out / f"{key}.jsonl")
        assert len(lines) >= 2
        assert nexts[0] <= instant(lines[1]["received_at"]) < nexts[0] + dt.timedelta(seconds=1.5)


def test_calendar_schedules_record_their_next_time(cluster, root, logs, daemon):
    out = output_dir(root)
    suffix = tag()
    rules = {
        "hourly": "@hourly",
        "half-past": "0 30 * * * *",
        "daily": "@daily",
        "monday": "0 15 10 * * 1",
    }
    for key, schedule in rules.items():
        daemon.add_rule(f"{key}.json", file_rule(f"Calendar {key} {suffix}", logs, INFO, out, f"{key}.jsonl", schedule=schedule))
    started = now()
    run_once(daemon, cluster, [cleaned(f"Calendar {key} {suffix}") for key in rules])
    finished = now()

    def following(moment, key):
        if key == "hourly":
            return moment.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)
        if key == "half-past":
            candidate = moment.replace(minute=30, second=0, microsecond=0)
            return candidate if candidate > moment else candidate + dt.timedelta(hours=1)
        if key == "daily":
            return moment.replace(hour=0, minute=0, second=0, microsecond=0) + dt.timedelta(days=1)
        candidate = moment.replace(hour=10, minute=15, second=0, microsecond=0) + dt.timedelta(days=(0 - moment.weekday()) % 7)
        return candidate if candidate > moment else candidate + dt.timedelta(days=7)

    for key in rules:
        (doc,) = cluster.state_docs(cleaned(f"Calendar {key} {suffix}"))
        _, next_query = check_state_doc(doc, cleaned(f"Calendar {key} {suffix}"), HEALTHY)
        assert next_query in {following(started, key), following(finished, key)}, (key, doc)
        # Every rule still runs once at start-up, whatever its schedule.
        assert len(read_lines(out / f"{key}.jsonl")) == 1


def test_restart_resumes_recorded_schedule(cluster, root, logs):
    out = output_dir(root)
    name = "Resume After Restart " + tag()
    first = Daemon(root, cluster)
    first.add_rule("rule.json", file_rule(name, logs, INFO, out, "alerts.jsonl"))
    run_once(first, cluster, [cleaned(name)])
    assert len(read_lines(out / "alerts.jsonl")) == 1
    cluster.refresh_state()

    second = Daemon(root, cluster)
    second.start()
    time.sleep(5)
    assert second.running()
    assert second.stop() == 0
    # next_query is an hour away: no new execution, alert or state document.
    assert len(read_lines(out / "alerts.jsonl")) == 1
    assert len(cluster.state_docs(cleaned(name))) == 1


def test_future_next_query_delays_first_execution(cluster, root, logs, indices, daemon):
    out = output_dir(root)
    name = "Delayed Start " + tag()
    seed = "go-es-alerts-status-0.0.3-seed-" + tag()
    indices.append(seed)
    due = now().replace(microsecond=0) + dt.timedelta(seconds=8)
    cluster.seed_state(seed, [{"@timestamp": rfc3339(now()), "rule_name": cleaned(name), "next_query": rfc3339(due), "hostname": "elsewhere", "hits_count": 0}])
    daemon.add_rule("rule.json", file_rule(name, logs, INFO, out, "alerts.jsonl"))
    daemon.start()
    time.sleep(4)
    assert not (out / "alerts.jsonl").exists()
    assert len(cluster.state_docs(cleaned(name))) == 1
    lines = wait_lines(out / "alerts.jsonl", 1, timeout=15)
    docs = wait_state(cluster, cleaned(name), 2)
    assert daemon.stop() == 0
    assert len(lines) == 1
    executed = instant(lines[0]["received_at"])
    assert due <= executed < due + dt.timedelta(seconds=2)
    written = [doc for doc in docs if doc.get("hostname") != "elsewhere"]
    assert len(written) == 1
    _, next_query = check_state_doc(written[0], cleaned(name), HEALTHY)
    assert 3599 <= (next_query - due).total_seconds() <= 3601


def test_state_lookup_selects_latest_exact_entry(cluster, root, logs, indices, daemon):
    out = output_dir(root)
    suffix = tag()
    seed = "go-es-alerts-status-0.0.3-seed-" + suffix
    indices.append(seed)
    moment = now().replace(microsecond=0)
    later = moment + dt.timedelta(hours=1)
    earlier = moment - dt.timedelta(hours=1)
    names = {key: f"Lookup {key} {suffix}" for key in ("past", "latest", "prefix", "unnormalized", "offset", "fraction", "fresh")}

    def entry(rule_name, next_query, written=moment):
        return {"@timestamp": rfc3339(written), "rule_name": rule_name, "next_query": next_query, "hostname": "elsewhere", "hits_count": 0}

    cluster.seed_state(
        seed,
        [
            entry(cleaned(names["past"]), rfc3339(earlier)),
            # The furthest next_query wins even when a later-written entry says otherwise.
            entry(cleaned(names["latest"]), rfc3339(later), written=moment - dt.timedelta(hours=2)),
            entry(cleaned(names["latest"]), rfc3339(moment - dt.timedelta(minutes=10))),
            entry(cleaned(names["prefix"]) + "-other", rfc3339(later)),
            entry(names["unnormalized"], rfc3339(later)),
            entry(cleaned(names["offset"]), later.astimezone(dt.timezone(dt.timedelta(hours=2))).isoformat()),
            entry(cleaned(names["fraction"]), later.strftime("%Y-%m-%dT%H:%M:%S.250Z")),
        ],
    )
    for key, name in names.items():
        daemon.add_rule(f"{key}.json", file_rule(name, logs, INFO, out, f"{key}.jsonl"))
    daemon.start()
    ran = {"past", "prefix", "unnormalized", "fresh"}
    for key in ran:
        assert len(wait_lines(out / f"{key}.jsonl", 1)) == 1, key
    time.sleep(3)
    assert daemon.stop() == 0
    assert {key for key in names if read_lines(out / f"{key}.jsonl")} == ran
    for key in names:
        own = [doc for doc in cluster.state_docs(cleaned(names[key])) if doc.get("hostname") != "elsewhere"]
        assert len(own) == (1 if key in ran else 0), (key, own)


def test_failed_state_write_stops_state_keeping(cluster, root, logs, daemon):
    out = output_dir(root)
    name = "Blocked State " + tag()
    moment = now()
    if moment.hour == 23 and moment.minute >= 59:
        time.sleep(61 - moment.second)
    today = "go-es-alerts-status-0.0.3-" + now().strftime("%Y.%m.%d")
    cluster.http("PUT", f"/{today}")
    status, body = cluster.http("PUT", f"/{today}/_settings", {"index.blocks.write": True})
    assert status == 200, body
    try:
        daemon.add_rule("rule.json", file_rule(name, logs, INFO, out, "alerts.jsonl", schedule="@every 2s"))
        daemon.start()
        assert len(wait_lines(out / "alerts.jsonl", 1)) == 1
        time.sleep(1)
    finally:
        cluster.http("PUT", f"/{today}/_settings", {"index.blocks.write": False})
    # Executions continue on schedule, but state is no longer written once a write failed.
    lines = wait_lines(out / "alerts.jsonl", 4, timeout=10)
    assert daemon.stop() == 0
    assert len(lines) >= 4
    assert cluster.state_docs(cleaned(name)) == []

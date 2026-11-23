"""Pre-fetching secrets into the cache at startup."""

import time

from conftest import assert_matches, assert_same_document, document, free_port, nested_config


def settle(recorder, op="BatchGetSecretValue", count=1, timeout=15):
    """Wait until `count` pre-fetch calls were answered, then return all startup calls."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with recorder.lock:
            seen = sum(1 for name, _ in recorder.calls if name == op)
        if seen >= count:
            break
        time.sleep(0.05)
    time.sleep(1.0)
    return recorder.take_calls()


def start(launch, recorder, extra, cache=""):
    port = free_port()
    config = nested_config(port, f"[capabilities.secrets_manager.cache]\nttl_seconds = 300\n{cache}" + extra)
    return launch(config, endpoint=recorder.url, port=port)


def test_prefetched_secret_served_from_cache(launch, recorder, secrets, sm):
    # Adapted from integration-tests/tests/prefetch.rs::test_prefetch_serves_cached_value.
    name = secrets.create("warm", SecretString='{"user":"testuser"}')["Name"]
    other = secrets.create("cold", SecretString="cold-1")["Name"]
    original = sm.get_secret_value(SecretId=name)
    program = start(launch, recorder, f'[[capabilities.secrets_manager.prefetch.secrets]]\nsecret_id = "{name}"\n')
    calls = settle(recorder)
    assert [op for op, _ in calls] == ["BatchGetSecretValue"]
    assert calls[0][1].get("SecretIdList") == [name]

    sm.put_secret_value(SecretId=name, SecretString='{"user":"updateduser"}')
    sm.put_secret_value(SecretId=other, SecretString="cold-2")
    warm = document(program.query(name))
    assert_matches(warm, original)
    assert_same_document(document(program.get("/v1/" + name)), warm)
    assert recorder.take() == []
    assert document(program.query(other))["SecretString"] == "cold-2"
    assert recorder.take() == ["GetSecretValue"]


def test_prefetch_entries_are_keyed_by_name(launch, recorder, secrets, sm):
    created = secrets.create("by-arn", SecretString="before")
    name, arn = created["Name"], created["ARN"]
    original = sm.get_secret_value(SecretId=name)
    program = start(launch, recorder, f'[capabilities.secrets_manager.prefetch]\nsecrets = [{{ secret_id = "{arn}" }}]\n')
    calls = settle(recorder)
    assert [op for op, _ in calls] == ["BatchGetSecretValue"] and calls[0][1].get("SecretIdList") == [arn]
    sm.put_secret_value(SecretId=name, SecretString="after")
    current = sm.get_secret_value(SecretId=name)

    # Configured by ARN, cached under the secret's name.
    assert_matches(document(program.query(name)), original)
    assert recorder.take() == []
    assert_matches(document(program.query(arn)), current)
    assert recorder.take() == ["GetSecretValue"]
    assert_matches(document(program.query(name, versionStage="AWSCURRENT")), current)
    assert recorder.take() == ["GetSecretValue"]


def test_prefetch_respects_cache_buffer_ratio(launch, recorder, secrets, sm):
    missing = secrets.name("missing")
    names = [secrets.create(f"ratio-{i}", SecretString=f"r{i}-old")["Name"] for i in range(4)]
    listed = [missing] + names
    entries = "".join(f'[[capabilities.secrets_manager.prefetch.secrets]]\nsecret_id = "{n}"\n' for n in listed)
    # 6 * 0.5 = 3 ids are requested, of which two exist.
    program = start(launch, recorder, "[capabilities.secrets_manager.prefetch]\ncache_buffer_ratio = 0.5\n" + entries, cache="cache_size = 6\n")
    calls = settle(recorder)
    assert [op for op, _ in calls] == ["BatchGetSecretValue"]
    assert calls[0][1].get("SecretIdList") == listed[:3]
    for i, name in enumerate(names):
        sm.put_secret_value(SecretId=name, SecretString=f"r{i}-new")
    assert [document(program.query(n))["SecretString"] for n in names] == ["r0-old", "r1-old", "r2-new", "r3-new"]
    assert recorder.take() == ["GetSecretValue", "GetSecretValue"]


def test_prefetch_batches_of_twenty(launch, recorder, secrets, sm):
    names = [secrets.create(f"bulk-{i:02d}", SecretString=f"b{i}-old")["Name"] for i in range(23)]
    entries = "".join(f'[[capabilities.secrets_manager.prefetch.secrets]]\nsecret_id = "{n}"\n' for n in names)
    program = start(launch, recorder, entries)
    calls = settle(recorder, count=2)
    assert [op for op, _ in calls] == ["BatchGetSecretValue", "BatchGetSecretValue"]
    assert [c[1].get("SecretIdList") for c in calls] == [names[:20], names[20:]]
    for name in names:
        sm.put_secret_value(SecretId=name, SecretString="changed")
    assert [document(program.query(n))["SecretString"] for n in names] == [f"b{i}-old" for i in range(23)]
    assert recorder.take() == []


def test_prefetch_by_tag(launch, recorder, secrets, sm):
    # prefetch.rs::test_prefetch_with_tags_serves_cached_value. Tag-key filters match by prefix.
    tag = secrets.name("tag")
    tagged = [secrets.create(f"tagged-{i}", SecretString=f"t{i}-old", Tags=[{"Key": tag, "Value": str(i)}])["Name"] for i in range(2)]
    plain = secrets.create("untagged", SecretString="u-old", Tags=[{"Key": "other-" + tag, "Value": "x"}])["Name"]
    program = start(launch, recorder, f'[capabilities.secrets_manager.prefetch]\nfilter_tags = [{{ key = "{tag}" }}]\n')
    calls = settle(recorder)
    assert [op for op, _ in calls] == ["BatchGetSecretValue"]
    assert calls[0][1].get("Filters") == [{"Key": "tag-key", "Values": [tag]}]
    for name in tagged + [plain]:
        sm.put_secret_value(SecretId=name, SecretString="new")
    assert [document(program.query(n))["SecretString"] for n in tagged] == ["t0-old", "t1-old"]
    assert recorder.take() == []
    assert document(program.query(plain))["SecretString"] == "new"


def test_prefetch_of_missing_secret_is_harmless(launch, recorder, secrets, sm):
    # prefetch.rs::test_prefetch_nonexistent_secret_doesnt_crash.
    missing = secrets.name("ghost")
    program = start(launch, recorder, f'[[capabilities.secrets_manager.prefetch.secrets]]\nsecret_id = "{missing}"\n')
    settle(recorder)
    assert program.get("/ping", token=False) == (200, "healthy")
    secrets.create("ghost", SecretString="materialized")
    assert_matches(document(program.query(missing)), sm.get_secret_value(SecretId=missing))


def test_top_level_prefetch_ignored_in_nested_layout(launch, recorder, secrets, sm):
    name = secrets.create("ignored", SecretString="first")["Name"]
    port = free_port()
    config = nested_config(port) + f'\n[prefetch]\nsecrets = [{{ secret_id = "{name}" }}]\n'
    program = launch(config, endpoint=recorder.url, port=port)
    time.sleep(1.5)
    assert recorder.take() == []
    sm.put_secret_value(SecretId=name, SecretString="second")
    assert document(program.query(name))["SecretString"] == "second"


def tag_filter(*keys):
    return [{"Key": "tag-key", "Values": [key]} for key in keys]


def batch_params(call):
    return {k: v for k, v in call[1].items() if k in ("SecretIdList", "Filters", "MaxResults")}


def cache_hits(program, recorder, names):
    """Names served without any AWS call (each lookup then caches the misses)."""
    recorder.take()
    found = []
    for name in names:
        document(program.query(name))
        if recorder.take() == []:
            found.append(name)
    return found


def first_page(sm, filters, limit):
    return [entry["Name"] for entry in sm.batch_get_secret_value(Filters=filters, MaxResults=limit)["SecretValues"]]


def test_tag_prefetch_respects_cache_buffer_ratio(launch, recorder, secrets, sm):
    tag = secrets.name("budget")
    tagged = [secrets.create(f"budget-{i}", SecretString="x", Tags=[{"Key": tag, "Value": "v"}])["Name"] for i in range(3)]
    # 4 * 0.5 = 2 secrets may be pre-fetched.
    program = start(
        launch, recorder,
        f'[capabilities.secrets_manager.prefetch]\ncache_buffer_ratio = 0.5\nfilter_tags = [{{ key = "{tag}" }}]\n',
        cache="cache_size = 4\n",
    )
    calls = settle(recorder)
    assert [op for op, _ in calls] == ["BatchGetSecretValue"]
    assert batch_params(calls[0]) == {"Filters": tag_filter(tag), "MaxResults": 2}
    expected = first_page(sm, tag_filter(tag), 2)
    assert len(expected) == 2
    assert cache_hits(program, recorder, tagged) == [n for n in tagged if n in expected]


def test_prefetch_budget_shared_by_ids_and_tags(launch, recorder, secrets, sm):
    tag = secrets.name("shared")
    tagged = [secrets.create(f"shared-{i}", SecretString="x", Tags=[{"Key": tag, "Value": "v"}])["Name"] for i in range(3)]
    explicit = [secrets.create(f"listed-{i}", SecretString="x")["Name"] for i in range(2)]
    missing = secrets.name("listed-missing")

    def config(ids):
        listed = ", ".join(f'{{ secret_id = "{n}" }}' for n in ids)
        return f'[capabilities.secrets_manager.prefetch]\ncache_buffer_ratio = 0.5\nsecrets = [{listed}]\nfilter_tags = [{{ key = "{tag}" }}]\n'

    # One listed id is fetched (the missing one costs nothing), leaving one slot for tags.
    program = start(launch, recorder, config([missing, explicit[0]]), cache="cache_size = 4\n")
    calls = settle(recorder, count=2)
    assert [op for op, _ in calls] == ["BatchGetSecretValue", "BatchGetSecretValue"]
    assert [batch_params(c) for c in calls] == [
        {"SecretIdList": [missing, explicit[0]]},
        {"Filters": tag_filter(tag), "MaxResults": 1},
    ]
    expected = first_page(sm, tag_filter(tag), 1)
    assert cache_hits(program, recorder, [explicit[0]] + tagged) == [explicit[0]] + [n for n in tagged if n in expected]
    program.stop()

    # Listed ids use the whole budget: no tag query is made.
    program = start(launch, recorder, config(explicit), cache="cache_size = 4\n")
    calls = settle(recorder)
    assert [batch_params(c) for c in calls] == [{"SecretIdList": explicit}]
    assert cache_hits(program, recorder, explicit + tagged) == explicit


def test_tag_prefetch_follows_pages(launch, recorder, secrets, sm):
    tag = secrets.name("pages")
    tagged = [secrets.create(f"pages-{i:02d}", SecretString="x", Tags=[{"Key": tag, "Value": "v"}])["Name"] for i in range(23)]
    program = start(launch, recorder, f'[capabilities.secrets_manager.prefetch]\nfilter_tags = [{{ key = "{tag}" }}]\n')
    calls = settle(recorder, count=2)
    assert [op for op, _ in calls] == ["BatchGetSecretValue", "BatchGetSecretValue"]
    assert [batch_params(c) for c in calls] == [{"Filters": tag_filter(tag), "MaxResults": 20}] * 2
    assert "NextToken" not in calls[0][1] and calls[1][1].get("NextToken")
    assert cache_hits(program, recorder, tagged) == tagged
    program.stop()

    # With a budget of 5 (10 * 0.5) one page of 5 is fetched and paging stops.
    program = start(
        launch, recorder,
        f'[capabilities.secrets_manager.prefetch]\ncache_buffer_ratio = 0.5\nfilter_tags = [{{ key = "{tag}" }}]\n',
        cache="cache_size = 10\n",
    )
    calls = settle(recorder)
    assert [batch_params(c) for c in calls] == [{"Filters": tag_filter(tag), "MaxResults": 5}]
    expected = first_page(sm, tag_filter(tag), 5)
    assert len(expected) == 5
    assert cache_hits(program, recorder, tagged) == [n for n in tagged if n in expected]


def test_multiple_tag_keys(launch, recorder, secrets, sm):
    first, second = secrets.name("key-one"), secrets.name("key-two")
    only_first = secrets.create("has-one", SecretString="x", Tags=[{"Key": first, "Value": "v"}])["Name"]
    only_second = secrets.create("has-two", SecretString="x", Tags=[{"Key": second, "Value": "v"}])["Name"]
    both = secrets.create("has-both", SecretString="x", Tags=[{"Key": first, "Value": "v"}, {"Key": second, "Value": "v"}])["Name"]
    program = start(launch, recorder, f'[capabilities.secrets_manager.prefetch]\nfilter_tags = [{{ key = "{first}" }}, {{ key = "{second}" }}]\n')
    calls = settle(recorder)
    assert [batch_params(c) for c in calls] == [{"Filters": tag_filter(first, second), "MaxResults": 20}]
    expected = first_page(sm, tag_filter(first, second), 20)
    assert expected == [both]
    assert cache_hits(program, recorder, [only_first, only_second, both]) == [both]

"""User Lua collection scripts (-script, -lua-script-read-only).

collect.lua and lists.lua adapt contrib/sample_collect_script.lua and
contrib/collect_lists_length_growing.lua from the upstream repository (MIT).
"""

import pytest

from harness import run_command, write_fixture
from metrics import assert_samples, select, value_of

SCRIPT_FAMILIES = {"script_values", "script_result"}

COLLECT = """-- Example collect script: alternating names and values, like HGETALL.
local result = {{}}
redis.call("SELECT", 2)
local r = redis.call("HGETALL", "{p}:stats")
if r ~= nil then
    for _,v in ipairs(r) do
        table.insert(result, v)
    end
end
table.insert(result, "foo")
table.insert(result, "42")
return result
"""

LISTS = """local result = {{}}
local function lengthOfList (key)
  return redis.call("LLEN", key)
end
redis.call("SELECT", 3)
local keysPresent = redis.call("KEYS", "{p}:q*")
if keysPresent ~= nil then
    for _,key in ipairs(keysPresent) do
        local status, retval = pcall(lengthOfList, key)
        if status == true then
            table.insert(result, "list_length_" .. key)
            table.insert(result, retval .. "")
        end
    end
end
return result
"""


def script(directory, name, body):
    path = directory / name
    write_fixture(path, body, mode=0o644)
    return path


def run(service, keyspace, scratch, *args):
    running = service("-redis.addr", keyspace.engine.url, *args, cwd=scratch)
    samples = running.samples()
    return running, samples


def scripts_and_keys(samples, prefix):
    return select(samples, lambda s: s[0] in SCRIPT_FAMILIES or any(prefix in v for v in s[1].values()))


def result(filename, code):
    return ("script_result", {"filename": str(filename)}, code)


def value(filename, key, number):
    return ("script_values", {"filename": str(filename), "key": key}, number)


def test_scripts_export_values_per_file(service, keyspace, scratch):
    p = keyspace.prefix
    keyspace.db(2).hset(f"{p}:stats", mapping={"hits": "10", "misses": "2.5"})
    keyspace.db(3).rpush(f"{p}:q1", "a", "b", "c")
    keyspace.db(3).rpush(f"{p}:q2", "a")
    keyspace.db(3).set(f"{p}:qs", "not a list")
    script(scratch, "collect.lua", COLLECT.format(p=p))
    lists = script(scratch, "lists.lua", LISTS.format(p=p))
    _, samples = run(service, keyspace, scratch, "-script", f"collect.lua,{lists}")
    assert value_of(samples, "up") == 1
    assert_samples(scripts_and_keys(samples, p), [
        result("collect.lua", 1),
        value("collect.lua", "hits", 10), value("collect.lua", "misses", 2.5), value("collect.lua", "foo", 42),
        result(lists, 1),
        value(lists, f"list_length_{p}:q1", 3), value(lists, f"list_length_{p}:q2", 1),
    ])


def test_repeated_names_keep_the_last_value(service, keyspace, scratch):
    p = keyspace.prefix
    script(scratch, "dup.lua", f'return {{"{p}_a", "1.5", "{p}_b", "2", "{p}_a", "7"}}')
    _, samples = run(service, keyspace, scratch, "-script", "dup.lua")
    assert value_of(samples, "up") == 1
    assert_samples(scripts_and_keys(samples, p), [
        result("dup.lua", 1), value("dup.lua", f"{p}_a", 7), value("dup.lua", f"{p}_b", 2),
    ])


def test_empty_script_result(service, keyspace, scratch):
    p = keyspace.prefix
    script(scratch, "empty.lua", "return {}")
    script(scratch, "one.lua", f'return {{"{p}_one", "1"}}')
    _, samples = run(service, keyspace, scratch, "-script", "empty.lua,one.lua")
    assert value_of(samples, "up") == 1
    assert_samples(scripts_and_keys(samples, p), [
        result("empty.lua", 2), result("one.lua", 1), value("one.lua", f"{p}_one", 1),
    ])


FAILING = {
    "non-numeric": 'return {{"{p}_x", "abc"}}',
    "odd-length": 'return {{"{p}_x", "1", "{p}_y"}}',
    "runtime-error": 'error("boom")',
    "not-a-table": "return 5",
    "integer-value": 'return {{"{p}_n", 3}}',
}


@pytest.mark.parametrize("body", FAILING.values(), ids=FAILING.keys())
def test_failing_script_marks_collection_down(service, keyspace, scratch, body):
    p = keyspace.prefix
    keyspace.db(0).set(f"{p}:k", "4")
    script(scratch, "bad.lua", body.format(p=p))
    _, samples = run(service, keyspace, scratch, "-script", "bad.lua", "-check-single-keys", f"{p}:k")
    assert value_of(samples, "up") == 0
    assert_samples(scripts_and_keys(samples, p), [
        result("bad.lua", 0),
        ("key_size", {"db": "db0", "key": f"{p}:k"}, 1),
        ("key_value", {"db": "db0", "key": f"{p}:k"}, 4),
    ])


def test_values_before_a_bad_value_may_remain(service, keyspace, scratch):
    p = keyspace.prefix
    script(scratch, "mixed.lua", f'return {{"{p}_x", "1", "{p}_y", "zz", "{p}_z", "2"}}')
    _, samples = run(service, keyspace, scratch, "-script", "mixed.lua")
    assert value_of(samples, "up") == 0
    got = scripts_and_keys(samples, p)
    assert [s for s in got if s[0] == "script_result"] == [result("mixed.lua", 0)]
    allowed = [value("mixed.lua", f"{p}_x", 1), value("mixed.lua", f"{p}_z", 2)]
    assert all(s in allowed for s in got if s[0] == "script_values"), got


def test_read_only_mode_rejects_writes(service, keyspace, scratch):
    p = keyspace.prefix
    r = keyspace.db(0)
    script(scratch, "write.lua", f'redis.call("SET", "{p}:written", "1"); return {{"{p}_w", "1"}}')
    script(scratch, "collect.lua", COLLECT.format(p=p))
    keyspace.db(2).hset(f"{p}:stats", mapping={"hits": "3"})

    _, samples = run(service, keyspace, scratch, "-script", "write.lua", "-lua-script-read-only")
    assert value_of(samples, "up") == 0
    assert_samples(scripts_and_keys(samples, p), [result("write.lua", 0)])
    assert r.exists(f"{p}:written") == 0

    _, samples = run(service, keyspace, scratch, "-script", "collect.lua", "-lua-script-read-only")
    assert value_of(samples, "up") == 1
    assert_samples(scripts_and_keys(samples, p), [
        result("collect.lua", 1), value("collect.lua", "hits", 3), value("collect.lua", "foo", 42),
    ])

    _, samples = run(service, keyspace, scratch, "-script", "write.lua")
    assert value_of(samples, "up") == 1
    assert_samples(scripts_and_keys(samples, p), [result("write.lua", 1), value("write.lua", f"{p}_w", 1)])
    assert r.get(f"{p}:written") == b"1"


def test_scripts_are_read_at_startup(service, keyspace, scratch):
    p = keyspace.prefix
    path = script(scratch, "s.lua", f'return {{"{p}_v", "1"}}')
    running, samples = run(service, keyspace, scratch, "-script", "s.lua")
    expected = [result("s.lua", 1), value("s.lua", f"{p}_v", 1)]
    assert_samples(scripts_and_keys(samples, p), expected)

    rewrite = run_command(["sh", "-c", 'printf %s "$1" > "$2"', "sh", f'return {{"{p}_v", "2", "{p}_new", "3"}}', str(path)],
                          cwd=scratch, timeout=10)
    assert rewrite.returncode == 0
    assert_samples(scripts_and_keys(running.samples(), p), expected)

    assert run_command(["rm", "-f", str(path)], cwd=scratch, timeout=10).returncode == 0
    samples = running.samples()
    assert value_of(samples, "up") == 1
    assert_samples(scripts_and_keys(samples, p), expected)

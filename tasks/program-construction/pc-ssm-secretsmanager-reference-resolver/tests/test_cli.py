"""Invocation, document input and output serialization of eval/get."""

import yaml

from checks import fails, json_stream, ok, same


def test_eval_json_and_yaml_render_the_same_resolved_data(aws, cli):
    host = aws.param("db/host", "db.internal")
    port = aws.param("db/port", "5432", "SecureString")
    text = f"""\
db:
  host: ref+awsssm://{host}
  port: ref+awsssm://{port}
  url: "postgres://ref+awsssm://{host}+:ref+awsssm://{port}+/app"
replicas: 3
enabled: true
servers: [ref+awsssm://{host}, 8080]
"""
    expected = {
        "db": {"host": "db.internal", "port": "5432", "url": "postgres://db.internal:5432/app"},
        "replicas": 3,
        "enabled": True,
        "servers": ["db.internal", 8080],
    }
    proc, docs = cli.eval(text, fmt="json")
    ok(proc)
    same(docs, [expected])
    proc, docs = cli.eval(text, fmt="yaml")
    ok(proc)
    same(docs, [expected])
    # YAML is the default output format.
    path = cli.file(text)
    proc = ok(cli.run("eval", "-s", "-f", path))
    same(list(yaml.safe_load_all(proc.stdout)), [expected])


def test_eval_reads_standard_input_by_default_or_with_dash(aws, cli):
    name = aws.param("a", "alpha")
    text = f'{{"j": "ref+awsssm://{name}", "k": [1, "ref+awsssm://{name}"]}}'
    for args in (["eval", "-s", "-o", "json"], ["eval", "-s", "-f", "-", "-o", "json"]):
        proc = ok(cli.run(*args, stdin=text))
        same(json_stream(proc.stdout), [{"j": "alpha", "k": [1, "alpha"]}])


def test_eval_directory_reads_the_documents_of_every_file(aws, cli):
    a = aws.param("a", "alpha")
    b = aws.param("b", "beta")
    cli.file(f"second: ref+awsssm://{b}\n", "docs/2-second.yaml")
    cli.file(f"first: ref+awsssm://{a}\n---\nalso: ref+awsssm://{b}\n", "docs/1-first.yaml")
    cli.file("c: 3\n---\nd: ref+awsssm://" + a + "\n", "docs/inner/deeper/y.yaml")
    proc = ok(cli.run("eval", "-s", "-f", cli.root / "docs", "-o", "json"))
    docs = json_stream(proc.stdout)
    # Subdirectories are read too. File order is not graded; documents within one file keep their order.
    expected = [{"first": "alpha"}, {"also": "beta"}, {"second": "beta"}, {"c": 3}, {"d": "alpha"}]
    assert len(docs) == len(expected), docs
    for group in ([{"first": "alpha"}, {"also": "beta"}], [{"second": "beta"}], [{"c": 3}, {"d": "alpha"}]):
        start = docs.index(group[0])
        same(docs[start:start + len(group)], group)
    remaining = list(docs)
    for want in expected:
        remaining.remove(want)
    assert remaining == []


def test_multi_document_stream_skips_empty_and_scalar_documents(aws, cli):
    name = aws.param("t", "T")
    text = f"""\
---
a: 1
---
---
{{}}
---
- ref+awsssm://{name}
- {{b: ref+awsssm://{name}}}
---
just a scalar
---
c: ref+awsssm://{name}
"""
    expected = [{"a": 1}, [f"ref+awsssm://{name}", {"b": "T"}], {"c": "T"}]
    proc, docs = cli.eval(text, fmt="json")
    ok(proc)
    same(docs, expected)
    # Documents are separated by a line holding only ---.
    assert [line for line in proc.stdout.splitlines() if line.strip() == "---"] == ["---", "---"]
    proc, docs = cli.eval(text, fmt="yaml")
    ok(proc)
    same(docs, expected)


def test_empty_input_produces_no_documents(cli):
    proc = ok(cli.run("eval", "-s", "-o", "json", stdin=""))
    assert proc.stdout.strip() == ""


def test_non_reference_values_keep_their_types(aws, cli):
    name = aws.param("a", "alpha")
    b = aws.param("b", "beta")
    text = f"""\
lit: [1, 2.5, true, false, null, "3", yes, "on", 0x10, 1e2, ~]
on: off
date: 2025-01-01
datetime: 2025-01-01T12:34:56Z
empty: ""
url: "$scheme://$host:$port/x"
multi: |
  line1 ref+awsssm://{name}
  line2 ref+awsssm://{b}
"""
    expected = {
        "lit": [1, 2.5, True, False, None, "3", "yes", "on", 16, 100, None],
        "on": "off",
        "date": "2025-01-01",
        "datetime": "2025-01-01T12:34:56Z",
        "empty": "",
        "url": "$scheme://$host:$port/x",
        "multi": "line1 alpha\nline2 beta\n",
    }
    for fmt in ("json", "yaml"):
        proc, docs = cli.eval(text, fmt=fmt)
        ok(proc)
        same(docs, [expected])


def test_references_resolve_at_any_depth_of_a_mapping_document(aws, cli):
    a = aws.param("a", "alpha")
    b = aws.param("b", "beta")
    text = f"""\
nested:
  - k: ref+awsssm://{a}
    inner: [ref+awsssm://{b}, {{z: ref+awsssm://{a}}}]
  - [ref+awsssm://{a}, [ref+awsssm://{b}]]
deep: {{x: {{y: {{z: ref+awsssm://{b}}}}}}}
"""
    proc, docs = cli.eval(text)
    ok(proc)
    same(docs, [{
        "nested": [{"k": "alpha", "inner": ["beta", {"z": "alpha"}]}, ["alpha", ["beta"]]],
        "deep": {"x": {"y": {"z": "beta"}}},
    }])


def test_top_level_sequence_only_resolves_inside_mappings(aws, cli):
    a = aws.param("a", "alpha")
    b = aws.param("b", "beta")
    text = f"""\
- ref+awsssm://{a}
- {{k: ref+awsssm://{a}}}
- [ref+awsssm://{a}, {{k: ref+awsssm://{b}}}]
- - - ref+awsssm://{b}
- 3
"""
    proc, docs = cli.eval(text)
    ok(proc)
    same(docs, [[
        f"ref+awsssm://{a}",
        {"k": "alpha"},
        [f"ref+awsssm://{a}", {"k": "beta"}],
        [[f"ref+awsssm://{b}"]],
        3,
    ]])


def test_any_failed_reference_fails_the_whole_run_without_output(aws, cli):
    a = aws.param("a", "alpha")
    text = f"a: ref+awsssm://{a}\n---\nb: ref+awsssm://{aws.prefix}/missing\n"
    for fmt in ("json", "yaml"):
        proc, _ = cli.eval(text, fmt=fmt)
        fails(proc)


def test_unreadable_or_malformed_input_fails(cli):
    fails(cli.run("eval", "-s", "-f", cli.root / "absent.yaml"))
    bad = cli.file("x: [unclosed\n")
    fails(cli.run("eval", "-s", "-f", bad, "-o", "json"))


def test_silent_flag_only_affects_standard_error(aws, cli):
    name = aws.param("a", "alpha")
    path = cli.file(f"a: ref+awsssm://{name}\n")
    quiet = ok(cli.run("eval", "-s", "-f", path, "-o", "json"))
    loud = ok(cli.run("eval", "-f", path, "-o", "json"))
    assert quiet.stderr == ""
    same(json_stream(quiet.stdout), [{"a": "alpha"}])
    same(json_stream(loud.stdout), [{"a": "alpha"}])
    quiet = ok(cli.run("get", "-s", f"ref+awsssm://{name}"))
    loud = ok(cli.run("get", f"ref+awsssm://{name}"))
    assert quiet.stderr == ""
    assert quiet.stdout == loud.stdout == "alpha"


def test_get_prints_the_expanded_string_without_newline(aws, cli):
    name = aws.param("n", "7")
    assert ok(cli.get(f"ref+awsssm://{name}")).stdout == "7"
    assert ok(cli.get("plain string, no references")).stdout == "plain string, no references"
    assert ok(cli.run("get", "-s", "--", f"<ref+awsssm://{name}+>")).stdout == "<7>"


def test_get_requires_a_non_empty_argument(cli):
    fails(cli.run("get", "-s"))
    fails(cli.run("get", "-s", ""))


def test_eval_missing_key_policy_flag(aws, cli):
    full, _ = aws.secret("app", '{"user": "u1"}')
    text = f"k: ref+awssecrets://{full}#/nope\nuser: ref+awssecrets://{full}#/user\nok: fine\n"
    proc, _ = cli.eval(text)
    fails(proc)
    proc, _ = cli.eval(text, "-fail-on-missing-key-in-map=true")
    fails(proc)
    for fmt in ("json", "yaml"):
        proc, docs = cli.eval(text, "-fail-on-missing-key-in-map=false", fmt=fmt)
        ok(proc)
        same(docs, [{"k": None, "user": "u1", "ok": "fine"}])

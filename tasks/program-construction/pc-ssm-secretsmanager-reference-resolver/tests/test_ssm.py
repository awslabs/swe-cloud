"""SSM Parameter Store references: single parameters, prefix maps, singleparam documents, versions, regions.

Prefix/fragment edge cases follow the upstream README SSM section; prefix shaping follows
pkg/providers/ssm/ssm_test.go. Recursive fixtures never hold both /x and /x/y (Floci's listing
order would decide which one wins).
"""

import pytest

from checks import fails, ok, same


def test_single_parameter_kinds(aws, cli):
    plain = aws.param("plain", "db.internal")
    secure = aws.param("secure", "s3cr3t", "SecureString")
    listed = aws.param("listed", "x,y,z", "StringList")
    number = aws.param("number", "5432")
    odd = aws.param("odd.name_with-chars", "odd")
    text = f"""\
plain: ref+awsssm://{plain}
secure: ref+awsssm://{secure}
listed: ref+awsssm://{listed}
number: ref+awsssm://{number}
odd: ref+awsssm://{odd}
slash: ref+awsssm:///{plain}
"""
    proc, docs = cli.eval(text)
    ok(proc)
    same(docs, [{"plain": "db.internal", "secure": "s3cr3t", "listed": "x,y,z", "number": "5432", "odd": "odd", "slash": "db.internal"}])


def test_missing_parameter_fails(aws, cli):
    aws.param("Name", "v")
    fails(cli.get(f"ref+awsssm://{aws.prefix}/absent"))
    fails(cli.get(f"ref+awsssm://{aws.prefix}/name"))
    # A path holding only children is not itself a parameter.
    aws.param("only/child", "c")
    fails(cli.get(f"ref+awsssm://{aws.prefix}/only"))


@pytest.fixture
def readme(aws):
    """/foo/bar = {"BAR":"VALUE"}, /foo/bar/a = A, /foo/bar/b = B, /foo/sec (SecureString)."""
    aws.param("foo/bar", '{"BAR":"VALUE"}')
    aws.param("foo/bar/a", "A")
    aws.param("foo/bar/b", "B")
    aws.param("foo/sec", "s3cr3t", "SecureString")
    return f"{aws.prefix}/foo"


def test_prefix_and_fragment_combinations_that_resolve(readme, cli):
    foo = readme
    text = f"""\
direct: ref+awsssm://{foo}/bar
by_prefix: ref+awsssm://{foo}#/bar
leaf: ref+awsssm://{foo}/bar/a
leaf_by_prefix: ref+awsssm://{foo}/bar?#/a
singleparam: ref+awsssm://{foo}/bar?mode=singleparam#/BAR
secure_by_prefix: ref+awsssm://{foo}#/sec
"""
    proc, docs = cli.eval(text)
    ok(proc)
    same(docs, [{
        "direct": '{"BAR":"VALUE"}',
        "by_prefix": '{"BAR":"VALUE"}',
        "leaf": "A",
        "leaf_by_prefix": "A",
        "singleparam": "VALUE",
        "secure_by_prefix": "s3cr3t",
    }])


def test_prefix_and_fragment_combinations_that_fail(readme, cli):
    foo = readme
    # /foo/bar as a prefix yields {"a": "A", "b": "B"}: no BAR key.
    proc, _ = cli.eval(f"v: ref+awsssm://{foo}/bar#/BAR\n")
    fails(proc)
    assert ok(cli.get(f"ref+awsssm://{foo}/bar#/BAR")).stdout == "<nil>"
    proc, docs = cli.eval(f"v: ref+awsssm://{foo}/bar#/BAR\n", "-fail-on-missing-key-in-map=false")
    ok(proc)
    same(docs, [{"v": None}])
    # Without recursion, /foo/bar is the string value, which cannot be descended into.
    for expression in (f"ref+awsssm://{foo}#/bar/a", f"ref+awsssm://{foo}#/sec/x", f"ref+awsssm://{foo}/bar#/a/b"):
        fails(cli.get(expression))
        proc, _ = cli.eval(f"v: {expression}\n", "-fail-on-missing-key-in-map=false")
        fails(proc)
    # An empty prefix is an error rather than a missing key.
    fails(cli.get(f"ref+awsssm://{foo}/nothing#/x"))
    proc, _ = cli.eval(f"v: ref+awsssm://{foo}/nothing#/x\n", "-fail-on-missing-key-in-map=false")
    fails(proc)


@pytest.fixture
def tree(aws):
    aws.param("top", "T")
    aws.param("r/x/y", "1")
    aws.param("r/x/z", "2")
    aws.param("r/w", "3")
    aws.param("r/q/s", "sec", "SecureString")
    return aws.prefix


def test_prefix_map_depth_depends_on_recursive(tree, cli):
    root = tree
    text = f"""\
top: ref+awsssm://{root}#/top
w: ref+awsssm://{root}/r#/w
w_false: ref+awsssm://{root}/r?recursive=false#/w
x: ref+awsssm://{root}/r?recursive=true#/x
y: ref+awsssm://{root}/r?recursive=true#/x/y
s: ref+awsssm://{root}/r?recursive=true#/q/s
all: ref+awsssm://{root}?recursive=true#/r
"""
    proc, docs = cli.eval(text)
    ok(proc)
    same(docs, [{
        "top": "T", "w": "3", "w_false": "3",
        "x": {"y": "1", "z": "2"}, "y": "1", "s": "sec",
        "all": {"x": {"y": "1", "z": "2"}, "w": "3", "q": {"s": "sec"}},
    }])
    # Non-recursive listing only holds direct children.
    proc, _ = cli.eval(f"x: ref+awsssm://{root}/r#/x\n")
    fails(proc)
    proc, docs = cli.eval(f"x: ref+awsssm://{root}/r#/x\nr: ref+awsssm://{root}#/r\n", "-fail-on-missing-key-in-map=false")
    ok(proc)
    same(docs, [{"x": None, "r": None}])
    assert ok(cli.get(f"ref+awsssm://{root}/r?recursive=true#/x")).stdout == "map[y:1 z:2]"


def test_prefix_map_errors(tree, cli):
    root = tree
    fails(cli.get(f"ref+awsssm://{root}/r?recursive=true#/x/y/z"))
    fails(cli.get(f"ref+awsssm://{root}/R?recursive=true#/x"))
    # Without a fragment recursive=true still reads the single parameter.
    fails(cli.get(f"ref+awsssm://{root}/r?recursive=true"))
    assert ok(cli.get(f"ref+awsssm://{root}/r/w?recursive=true")).stdout == "3"


def test_singleparam_document(aws, cli):
    json_doc = aws.param("json", '{"port": 5432, "on": true, "f": 2.5, "s": "str", "nil": null, "l": [1, "two"], "m": {"k": "v"}}')
    yaml_doc = aws.param("yaml", "a: 1\nb: [x, y]\nf: \"07\"\ng: yes\nm: {k: v, n: 2}\n", "SecureString")
    j = f"ref+awsssm://{json_doc}?mode=singleparam#"
    y = f"ref+awsssm://{yaml_doc}?mode=singleparam#"
    text = f"""\
port: {j}/port
on: {j}/on
f: {j}/f
s: {j}/s
nil: {j}/nil
l: {j}/l
mk: {j}/m/k
a: {y}/a
b: {y}/b
f07: {y}/f
g: {y}/g
m: {y}/m
raw: ref+awsssm://{json_doc}?mode=singleparam
"""
    proc, docs = cli.eval(text)
    ok(proc)
    same(docs, [{
        "port": 5432, "on": True, "f": 2.5, "s": "str", "nil": None, "l": [1, "two"], "mk": "v",
        "a": 1, "b": ["x", "y"], "f07": "07", "g": "yes", "m": {"k": "v", "n": 2},
        "raw": '{"port": 5432, "on": true, "f": 2.5, "s": "str", "nil": null, "l": [1, "two"], "m": {"k": "v"}}',
    }])


def test_singleparam_failures(aws, cli):
    text_param = aws.param("text", "just text")
    doc = aws.param("doc", '{"l": [1, 2], "k": "v"}')
    fails(cli.get(f"ref+awsssm://{text_param}?mode=singleparam#/x"))
    fails(cli.get(f"ref+awsssm://{doc}?mode=singleparam#/l/0"))
    fails(cli.get(f"ref+awsssm://{aws.prefix}/absent?mode=singleparam#/k"))
    proc, _ = cli.eval(f"v: ref+awsssm://{doc}?mode=singleparam#/zz\n")
    fails(proc)
    assert ok(cli.get(f"ref+awsssm://{doc}?mode=singleparam#/zz")).stdout == "<nil>"


def test_parameter_versions(aws, cli):
    name = aws.param("ver", "one")
    aws.param("ver", "two", overwrite=True)
    aws.param("ver", '{"k": "three"}', overwrite=True)
    text = f"""\
v1: ref+awsssm://{name}?version=1
v2: ref+awsssm://{name}?version=2
current: ref+awsssm://{name}
k: ref+awsssm://{name}?mode=singleparam#/k
k3: ref+awsssm://{name}?mode=singleparam&version=3#/k
"""
    proc, docs = cli.eval(text)
    ok(proc)
    same(docs, [{"v1": "one", "v2": "two", "current": '{"k": "three"}', "k": "three", "k3": "three"}])
    fails(cli.get(f"ref+awsssm://{name}?version=9"))
    fails(cli.get(f"ref+awsssm://{name}?version=abc"))
    fails(cli.get(f"ref+awsssm://{name}?mode=singleparam&version=2#/k"))


def test_version_is_ignored_for_prefix_maps(aws, cli):
    aws.param("p/sec", "first")
    aws.param("p/sec", "second", overwrite=True)
    assert ok(cli.get(f"ref+awsssm://{aws.prefix}/p?version=1#/sec")).stdout == "second"
    assert ok(cli.get(f"ref+awsssm://{aws.prefix}/p/sec?version=1")).stdout == "first"


def test_region_parameter_selects_the_region(aws, cli):
    west = aws.param("west", "W", region="us-west-2")
    east = aws.param("east", "E")
    fails(cli.get(f"ref+awsssm://{west}"))
    proc, docs = cli.eval(f"w: ref+awsssm://{west}?region=us-west-2\ne: ref+awsssm://{east}\nwp: ref+awsssm://{aws.prefix}?region=us-west-2#/west\n")
    ok(proc)
    same(docs, [{"w": "W", "e": "E", "wp": "W"}])
    fails(cli.get(f"ref+awsssm://{east}?region=us-west-2"))


def test_large_prefix_map_is_complete(aws, cli):
    # 15 direct children plus 12 SecureStrings one level deeper (more than one default AWS page).
    for n in range(15):
        aws.param(f"g/p/k{n:02d}", f"v{n}")
    for n in range(12):
        aws.param(f"g/p/sub/k{n:02d}", f"s{n}", "SecureString")
    flat = "".join(f"k{n:02d}: ref+awsssm://{aws.prefix}/g/p#/k{n:02d}\n" for n in range(15))
    proc, docs = cli.eval(flat + f"sub: ref+awsssm://{aws.prefix}/g/p#/sub\n", "-fail-on-missing-key-in-map=false")
    ok(proc)
    same(docs, [{**{f"k{n:02d}": f"v{n}" for n in range(15)}, "sub": None}])
    proc, docs = cli.eval(f"all: ref+awsssm://{aws.prefix}/g?recursive=true#/p\n")
    ok(proc)
    same(docs, [{"all": {**{f"k{n:02d}": f"v{n}" for n in range(15)}, "sub": {f"k{n:02d}": f"s{n}" for n in range(12)}}}])

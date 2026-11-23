"""Reference-expression grammar: delimiting, embedding, secretref, nesting and rendering of values.

Several cases adapt upstream pkg/expansion/expand_match_test.go (echo provider replaced by SSM).
"""

import pytest

from checks import fails, ok, same


@pytest.fixture
def ab(aws):
    return aws.param("a", "alpha"), aws.param("b", "beta")


def test_expression_boundaries(ab, cli):
    a, b = ab
    cases = {
        f"a ref+awsssm://{a}+ b ref+awsssm://{b}+ c": "a alpha b beta c",
        f"ref+awsssm://{a} ref+awsssm://{b}": "alpha beta",
        f'"ref+awsssm://{a}"': '"alpha"',
        f"ref+awsssm://{a}+ + + ": "alpha + + ",
        f"ref+awsssm://{a}+ref+awsssm://{b}": "alphabeta",
        f"ref+awsssm://{a}+-ref+awsssm://{b}+": "alpha-beta",
        f"pre-ref+awsssm://{a}": "pre-alpha",
        f"ref+awsssm:/{a}": f"ref+awsssm:/{a}",
        "return 301 $scheme://$host:$server_port/remote.php/dav;": "return 301 $scheme://$host:$server_port/remote.php/dav;",
        f"ref+awsssm://{a}#": "alpha",
        f"ref+awsssm://{a}?": "alpha",
        f"line ref+awsssm://{a}\nnext ref+awsssm://{b}\n": "line alpha\nnext beta\n",
    }
    for expression, expected in cases.items():
        proc = ok(cli.get(expression))
        assert proc.stdout == expected, (expression, proc.stdout)


def test_comma_does_not_end_an_expression_followed_by_more_text(ab, cli):
    a, b = ab
    fails(cli.get(f"ref+awsssm://{a},ref+awsssm://{b}"))
    # A trailing comma alone is not part of the expression.
    assert ok(cli.get(f"ref+awsssm://{a},")).stdout == "alpha,"
    assert ok(cli.get(f"ref+awsssm://{a}+,ref+awsssm://{b}")).stdout == "alpha,beta"


def test_secretref_resolves_unless_excluded(ab, cli):
    a, b = ab
    text = f"""\
sec: secretref+awsssm://{a}
plain: ref+awsssm://{b}
secret_first: "secretref+awsssm://{a}+ and ref+awsssm://{b}+"
ref_first: "ref+awsssm://{b}+ and secretref+awsssm://{a}+"
items: [secretref+awsssm://{a}, ref+awsssm://{b}]
"""
    proc, docs = cli.eval(text)
    ok(proc)
    same(docs, [{
        "sec": "alpha", "plain": "beta", "secret_first": "alpha and beta",
        "ref_first": "beta and alpha", "items": ["alpha", "beta"],
    }])
    proc, docs = cli.eval(text, "-exclude-secret")
    ok(proc)
    same(docs, [{
        "sec": f"secretref+awsssm://{a}",
        "plain": "beta",
        "secret_first": f"secretref+awsssm://{a}+ and ref+awsssm://{b}+",
        "ref_first": f"beta and secretref+awsssm://{a}+",
        "items": [f"secretref+awsssm://{a}", "beta"],
    }])
    assert ok(cli.get(f"secretref+awsssm://{a}")).stdout == "alpha"


def test_nested_reference_supplies_the_outer_path(aws, ab, cli):
    a, _ = ab
    pointer = aws.param("ptr", a)
    assert ok(cli.get(f"ref+awsssm://ref+awsssm://{pointer}")).stdout == "alpha"
    proc, docs = cli.eval(f"n: ref+awsssm://ref+awsssm://{pointer}?x=1\nm: x-ref+awsssm://ref+awsssm://{pointer}++-y\n")
    ok(proc)
    same(docs, [{"n": "alpha", "m": "x-alpha-y"}])
    # The inner expression consumes the first '+', so the outer path continues into "-y".
    fails(cli.get(f"x-ref+awsssm://ref+awsssm://{pointer}+-y"))


def test_whole_value_keeps_type_while_embedded_value_is_rendered_as_text(aws, cli):
    doc = aws.param("doc", '{"port": 5432, "on": true, "f": 2.5, "s": "str", "nil": null, "l": [1, "two"], "m": {"k": "v", "n": 2}}')
    r = f"ref+awsssm://{doc}?mode=singleparam#"
    text = f"""\
port: {r}/port
on: {r}/on
f: {r}/f
nil: {r}/nil
l: {r}/l
m: {r}/m
emb_port: "p={r}/port+"
emb_on: "{r}/on+!"
emb_f: "f={r}/f+"
emb_nil: "x={r}/nil+"
emb_l: "l={r}/l+"
emb_m: "<{r}/m+>"
joined: "port={r}/port+,on={r}/on+,f={r}/f+"
spaced: "l={r}/l+ m={r}/m+"
"""
    expected = {
        "port": 5432, "on": True, "f": 2.5, "nil": None, "l": [1, "two"], "m": {"k": "v", "n": 2},
        "emb_port": "p=5432", "emb_on": "true!", "emb_f": "f=2.5", "emb_nil": "x=<nil>",
        "emb_l": "l=[1 two]", "emb_m": "<map[k:v n:2]>", "joined": "port=5432,on=true,f=2.5",
        "spaced": "l=[1 two] m=map[k:v n:2]",
    }
    for fmt in ("json", "yaml"):
        proc, docs = cli.eval(text, fmt=fmt)
        ok(proc)
        same(docs, [expected])
    assert ok(cli.get(f"{r}/m")).stdout == "map[k:v n:2]"
    assert ok(cli.get(f"{r}/l")).stdout == "[1 two]"


def test_adjacent_embedded_reference_must_be_scalar(aws, cli):
    doc = aws.param("doc", '{"port": 5432, "l": [1, "two"]}')
    r = f"ref+awsssm://{doc}?mode=singleparam#"
    # Without whitespace the later reference is resolved first, and it must be a scalar.
    fails(cli.get(f"p={r}/port+;l={r}/l+"))
    assert ok(cli.get(f"l={r}/l+;p={r}/port+")).stdout == "l=[1 two];p=5432"

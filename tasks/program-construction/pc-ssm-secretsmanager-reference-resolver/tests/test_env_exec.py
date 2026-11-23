"""env and exec: resolving a flat variable map into shell lines or a child environment.

Quoting cases adapt upstream vals_test.go TestQuotedEnv.
"""

from collections import Counter

import pytest

from checks import fails, ok


def lines(text):
    return Counter(text.splitlines())


@pytest.fixture
def variables(aws, cli):
    a = aws.param("a", "alpha")
    sp = aws.param("sp", "has space; and 'q'")
    full, _ = aws.secret("app", '{"pw": "p2"}')
    path = cli.file(f"""\
A: ref+awsssm://{a}
SP: ref+awsssm://{sp}
PW: ref+awssecrets://{full}#/pw
PLAIN: "x y"
SAFE: "a@b%c+d=e:f,g.h/i-j_k"
EMPTY: ""
QUOTE: "it's"
LEAD: "'value"
DOLLAR: "$HOME"
TAB: "a\\tb"
NL: "l1\\nl2"
EMB: "pw=ref+awssecrets://{full}#/pw+"
""", "vars.yaml")
    return path


ENV_LINES = [
    "A=alpha",
    "SP='has space; and '\"'\"'q'\"'\"''",
    "PW=p2",
    "PLAIN='x y'",
    "SAFE=a@b%c+d=e:f,g.h/i-j_k",
    "EMPTY=",
    "QUOTE='it'\"'\"'s'",
    "LEAD=''\"'\"'value'",
    "DOLLAR='$HOME'",
    "TAB='a\tb'",
    "NL='l1",
    "l2'",
    "EMB=pw=p2",
]


def test_env_prints_shell_quoted_assignments(variables, cli):
    proc = ok(cli.run("env", "-f", variables))
    assert lines(proc.stdout) == Counter(ENV_LINES)


def test_env_export_prefix(variables, cli):
    proc = ok(cli.run("env", "-export", "-f", variables))
    expected = [line if line == "l2'" else "export " + line for line in ENV_LINES]
    assert lines(proc.stdout) == Counter(expected)


def test_env_reads_standard_input_and_only_the_first_document(aws, cli):
    a = aws.param("a", "alpha")
    proc = ok(cli.run("env", "-f", "-", stdin=f"A: ref+awsssm://{a}\n---\nB: ref+awsssm://{a}\n"))
    assert lines(proc.stdout) == Counter(["A=alpha"])


def test_env_rejects_non_string_values(aws, cli):
    doc = aws.param("doc", '{"port": 5432, "k": "v"}')
    full, _ = aws.secret("s", '{"k": "v"}')
    for text in ("N: 3\n", "F: true\n", "M: {a: 1}\n", "L: [a]\n", "Z: null\n",
                 f"P: ref+awsssm://{doc}?mode=singleparam#/port\n", f"X: ref+awssecrets://{full}#/missing\n",
                 "- a\n"):
        fails(cli.run("env", "-f", cli.file(text)))
    fails(cli.run("env"))
    fails(cli.run("env", "-f", cli.file(f"A: ref+awsssm://{aws.prefix}/absent\n")))


def test_exec_child_sees_only_resolved_variables(variables, cli):
    proc = ok(cli.run("exec", "-s", "-f", variables, "--", "env", env={"MARK": "m"}))
    assert proc.stderr == ""
    unquoted = ["A=alpha", "SP=has space; and 'q'", "PW=p2", "PLAIN=x y", "SAFE=a@b%c+d=e:f,g.h/i-j_k", "EMPTY=",
                "QUOTE=it's", "LEAD='value", "DOLLAR=$HOME", "TAB=a\tb", "NL=l1", "l2", "EMB=pw=p2"]
    assert lines(proc.stdout) == Counter(unquoted)


def test_exec_inherit_merges_caller_environment(aws, cli):
    a = aws.param("a", "alpha")
    path = cli.file(f"A: ref+awsssm://{a}\nB: plain\n")
    proc = ok(cli.run("exec", "-s", "-i", "-f", path, "--", "env", env={"MARK": "m", "A": "outer"}))
    out = proc.stdout.splitlines()
    assert "MARK=m" in out and "B=plain" in out
    assert [line for line in out if line.startswith("A=")] == ["A=alpha"]
    assert any(line.startswith("PATH=") for line in out)


def test_exec_without_file_inherits_caller_environment(cli):
    proc = ok(cli.run("exec", "-s", "--", "sh", "-c", 'printf "%s" "$MARK"', env={"MARK": "marked"}))
    assert proc.stdout == "marked"


def test_exec_passes_arguments_streams_and_stdin(aws, cli):
    a = aws.param("a", "alpha")
    path = cli.file(f"A: ref+awsssm://{a}\n")
    proc = ok(cli.run("exec", "-s", "-f", path, "--", "printf", "%s|", "a b", "c"))
    assert proc.stdout == "a b|c|"
    proc = ok(cli.run("exec", "-s", "-f", path, "--", "sh", "-c", 'read line; echo "got $line A=$A"; echo err >&2', stdin="piped\n"))
    assert proc.stdout == "got piped A=alpha\n"
    assert proc.stderr == "err\n"
    proc = ok(cli.run("exec", "-s", "-f", path, "printf", "%s", "nodash"))
    assert proc.stdout == "nodash"


def test_exec_failures(aws, cli):
    a = aws.param("a", "alpha")
    good = cli.file(f"A: ref+awsssm://{a}\n")
    fails(cli.run("exec", "-s", "-f", good, "--", "sh", "-c", "exit 3"))
    fails(cli.run("exec", "-s", "-f", good))
    fails(cli.run("exec", "-s", "-f", good, "--", "/nonexistent/command"))
    marker = cli.root / "ran"
    for text in (f"A: ref+awsssm://{aws.prefix}/absent\n", "N: 3\n"):
        proc = cli.run("exec", "-s", "-f", cli.file(text), "--", "sh", "-c", f"echo ran; touch {marker}")
        fails(proc)
        assert not marker.exists()

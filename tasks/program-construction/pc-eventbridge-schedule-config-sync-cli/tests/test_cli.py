"""Invocation surface: flags, subcommands, exit codes and flag placement."""

from support import cli, rule_names, rule_snapshot, write


def rule_config(ns, *names, tracking=True):
    body = "".join(
        f"""  - name: {name}
    scheduleExpression: rate(1 hour)
    state: ENABLED
    targets:
      - id: q
        arn: {ns.sqs()}
"""
        for name in names
    )
    head = f"region: us-east-1\n" + (f"trackingId: {ns.tracking}\n" if tracking else "")
    return head + "rules:\n" + body


def test_version(ws):
    result = cli("-version", cwd=ws)
    assert (result.returncode, result.stdout) == (0, "ebschedule dev\n")


def test_usage_exit_codes(ws):
    helped = cli("-h", cwd=ws)
    assert helped.returncode == 0 and helped.stdout == ""
    assert helped.stderr.startswith("usage:\n"), helped.stderr
    for args in ([], ["bogus"]):
        result = cli(*args, cwd=ws)
        assert result.returncode == 1, args
        assert result.stdout == "" and result.stderr.startswith("usage:\n"), result.stderr
    unknown = cli("-nope", "diff", cwd=ws)
    assert unknown.returncode == 2 and unknown.stdout == ""
    # The flag parser's wording is not graded; it must name the flag and show the usage text.
    assert "-nope" in unknown.stderr and "usage:" in unknown.stderr.splitlines(), unknown.stderr


def test_malformed_flag_values_are_parse_errors(ws, ns):
    write(ws, "ebschedule.yaml", rule_config(ns, f"{ns.p}-r"))
    for args in (["-target", "rule", "apply"], ["-target", "lambda:x", "apply"], ["-target", "rule:", "apply"], ["-tag", "novalue", "dump"]):
        result = cli("-auto-approve", *args, cwd=ws)
        assert result.returncode == 2, (args, result.stderr)
        assert result.stdout == ""
        assert args[1] in result.stderr and "usage:" in result.stderr.splitlines(), result.stderr
    assert rule_names(ns.p) == []


def test_default_config_file_and_missing_config(ws, ns):
    missing = cli("diff", cwd=ws)
    assert (missing.returncode, missing.stdout, missing.stderr) == (1, "", "error: no config files match: ebschedule.yaml\n")
    globbed = cli("-conf", "conf/*.yaml", "validate", cwd=ws)
    assert (globbed.returncode, globbed.stderr) == (1, "error: no config files match: conf/*.yaml\n")
    applied = cli("-conf", "nope.yaml", "-auto-approve", "apply", cwd=ws)
    assert (applied.returncode, applied.stderr) == (1, "error: no config files match: nope.yaml\n")

    write(ws, "ebschedule.yaml", rule_config(ns, f"{ns.p}-r"))
    result = cli("-auto-approve", "apply", cwd=ws)
    assert (result.returncode, result.stdout) == (0, f"+ rule:{ns.p}-r (create)\n"), result.stderr
    assert rule_names(ns.p) == [f"{ns.p}-r"]


def test_flags_after_subcommand_are_not_flags(ws, ns):
    write(ws, "c.yaml", rule_config(ns, f"{ns.p}-a", f"{ns.p}-b"))
    # "-dry-run" after the subcommand is a positional argument: apply really happens.
    result = cli("-conf", "c.yaml", "apply", "-dry-run", cwd=ws)
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"+ rule:{ns.p}-a (create)\n+ rule:{ns.p}-b (create)\n"
    assert rule_names(ns.p) == [f"{ns.p}-a", f"{ns.p}-b"]

    # "-target" after diff does not restrict the output.
    write(ws, "d.yaml", rule_config(ns, f"{ns.p}-a", f"{ns.p}-c"))
    diffed = cli("-conf", "d.yaml", "diff", "-target", f"rule:{ns.p}-a", cwd=ws)
    assert diffed.returncode == 2
    assert [line for line in diffed.stdout.splitlines() if line.startswith("+++ ")] == [f"+++ rule:{ns.p}-c (desired)"]

    # "-prune" after apply does not prune.
    write(ws, "e.yaml", rule_config(ns, f"{ns.p}-a"))
    pruned = cli("-conf", "e.yaml", "apply", "-prune", cwd=ws)
    assert (pruned.returncode, pruned.stdout) == (0, f"= rule:{ns.p}-a (no-op)\n")
    assert rule_names(ns.p) == [f"{ns.p}-a", f"{ns.p}-b"]


def test_apply_without_terminal_does_not_prompt(ws, ns):
    write(ws, "c.yaml", rule_config(ns, f"{ns.p}-r"))
    result = cli("-conf", "c.yaml", "apply", cwd=ws, stdin="no\n")
    assert (result.returncode, result.stdout, result.stderr) == (0, f"+ rule:{ns.p}-r (create)\n", "")
    assert rule_snapshot(f"{ns.p}-r")["rule"]["State"] == "ENABLED"


def test_target_restrictions(ws, ns):
    write(ws, "c.yaml", rule_config(ns, f"{ns.p}-a", f"{ns.p}-b"))
    exclusive = cli("-conf", "c.yaml", "-target", f"rule:{ns.p}-a", "-prune", "-auto-approve", "apply", cwd=ws)
    assert (exclusive.returncode, exclusive.stdout, exclusive.stderr) == (1, "", "error: -target and -prune are mutually exclusive\n")
    assert rule_names(ns.p) == []

    unknown = cli("-conf", "c.yaml", "-target", f"rule:{ns.p}-a", "-target", "schedule:nothere", "-auto-approve", "apply", cwd=ws)
    assert (unknown.returncode, unknown.stdout, unknown.stderr) == (1, "", "error: -target schedule:nothere not found in config\n")
    assert rule_names(ns.p) == []
    for args in (["diff"], ["-dry-run", "apply"]):
        result = cli("-conf", "c.yaml", "-target", "rule:nothere", *args, cwd=ws)
        assert (result.returncode, result.stderr) == (1, "error: -target rule:nothere not found in config\n")

    only = cli("-conf", "c.yaml", "-target", f"rule:{ns.p}-b", "-auto-approve", "apply", cwd=ws)
    assert (only.returncode, only.stdout) == (0, f"+ rule:{ns.p}-b (create)\n"), only.stderr
    assert rule_names(ns.p) == [f"{ns.p}-b"]

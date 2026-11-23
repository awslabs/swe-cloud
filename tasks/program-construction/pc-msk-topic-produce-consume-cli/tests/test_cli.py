"""Invocation: broker configuration sources and refused command lines."""

from harness import run_program, workspace, write_fixture


def consume_args(topic):
    return ["-C", "-t", topic, "-o", "beginning", "-e", "-f", "%s\\n"]


def plain_topic(kafka):
    topic = kafka.topic()
    kafka.produce(topic, [{"partition": 0, "value": b"a"}, {"partition": 0, "value": b"b"}])
    return topic


def run_without_b(args, home, cwd=None, env=None):
    return run_program(args, cwd=cwd, env={"HOME": str(home), **(env or {})}, text=False)


def test_configuration_file_option(kafka):
    topic = plain_topic(kafka)
    with workspace() as root:
        write_fixture(root / "cfg", f"bootstrap.servers={kafka.bootstrap}\n")
        # Java-style escaped colons are accepted; a configuration file wins over KCAT_CONFIG.
        write_fixture(root / "escaped", "bootstrap.servers=" + kafka.bootstrap.replace(":", "\\:") + "\n")
        for name in ("cfg", "escaped"):
            result = run_without_b(["-F", name, *consume_args(topic)], kafka.home, cwd=root, env={"KCAT_CONFIG": "/nonexistent/x.conf"})
            assert result.returncode == 0, (name, result.stderr)
            assert result.stdout == b"a\nb\n"
        write_fixture(root / "broken", f"bootstrap.servers {kafka.bootstrap}\n")
        for name in ("broken", "absent"):
            result = run_without_b(["-F", name, *consume_args(topic)], kafka.home, cwd=root)
            assert result.returncode == 1, name
            assert result.stdout == b""


def test_environment_configuration(kafka):
    topic = plain_topic(kafka)
    with workspace() as root:
        write_fixture(root / "cfg", f"bootstrap.servers={kafka.bootstrap}\n")
        result = run_without_b(consume_args(topic), kafka.home, env={"KCAT_CONFIG": str(root / "cfg")})
        assert result.returncode == 0, result.stderr
        assert result.stdout == b"a\nb\n"
        missing = kafka.run(*consume_args(topic), env={"KCAT_CONFIG": str(root / "absent")})
        assert missing.returncode == 1 and missing.stdout == b""


def test_home_configuration_file(kafka):
    topic = plain_topic(kafka)
    with workspace() as home:
        conf = home / ".config" / "kcat.conf"
        write_fixture(conf, f"# comment\n\n  bootstrap.servers={kafka.bootstrap}  \n")
        result = run_without_b(consume_args(topic), home)
        assert result.returncode == 0, result.stderr
        assert result.stdout == b"a\nb\n"
        write_fixture(conf, "bogus.prop=1\n")
        bad = run_program(["-b", kafka.bootstrap, *consume_args(topic)], env={"HOME": str(home)}, text=False)
        assert bad.returncode == 1 and bad.stdout == b""
        # -F none skips the default file; KCAT_CONFIG replaces it.
        skipped = run_program(["-F", "none", "-b", kafka.bootstrap, *consume_args(topic)], env={"HOME": str(home)}, text=False)
        assert skipped.returncode == 0, skipped.stderr
        assert skipped.stdout == b"a\nb\n"
        write_fixture(home / "cfg", f"bootstrap.servers={kafka.bootstrap}\n")
        replaced = run_without_b(consume_args(topic), home, env={"KCAT_CONFIG": str(home / "cfg")})
        assert replaced.returncode == 0, replaced.stderr
        assert replaced.stdout == b"a\nb\n"


def test_bootstrap_property_instead_of_b(kafka):
    topic = plain_topic(kafka)
    result = run_without_b(["-X", f"bootstrap.servers={kafka.bootstrap}", *consume_args(topic)], kafka.home)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"a\nb\n"


def test_refused_command_lines(kafka):
    topic = plain_topic(kafka)
    refusals = [
        ["-C", "-t", topic],  # no broker list
    ]
    for args in refusals:
        result = run_without_b(args, kafka.home)
        assert result.returncode == 1, args
        assert result.stdout == b""
    with_brokers = [
        ["-C"],
        ["-P"],
        ["-C", "-t", topic, "-W"],
        ["-C", "-t", topic, "extra-operand"],
        ["-X", "nosuch.property=1", *consume_args(topic)],
        ["-X", "noequals", *consume_args(topic)],
        ["-o", "stored", "-C", "-t", topic, "-e"],
    ]
    for args in with_brokers:
        result = kafka.run(*args)
        assert result.returncode == 1, args
        assert result.stdout == b"", args
    assert [r["value"] for r in kafka.records(topic)[0]] == [b"a", b"b"]


def test_help_exits_successfully(kafka):
    assert run_without_b(["-h"], kafka.home).returncode == 0

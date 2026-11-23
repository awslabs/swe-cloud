"""Invocation: offline checking, validation failures, variables, file inclusion, credentials and exit status."""

import pytest
from dnstest import HEADER, corrections, failed, ok, rr

UNREACHABLE = {"AWS_ENDPOINT_URL": "http://127.0.0.1:9"}


def test_check_needs_no_provider(project, zones):
    zone = zones("chk")
    project.config(f'D("{zone.name}", REG_NONE, DnsProvider(R53), A("n", "192.0.2.1"));\n')
    ok(project.run("check", env=UNREACHABLE))
    failed(project.run("preview", env=UNREACHABLE))
    assert zone.state() == zone.initial


INVALID = {
    "ipv4-word": 'A("bad", "not-an-ip")',
    "ipv4-octet": 'A("bad", "192.0.2.256")',
    "ipv6-digits": 'AAAA("bad", "2001:db8::zz")',
    "ipv6-given-ipv4": 'AAAA("bad", "192.0.2.1")',
    "cname-dotted-relative": 'CNAME("c", "foo.bar")',
    "mx-dotted-relative": 'MX("@", 10, "foo.bar")',
    "srv-dotted-relative": 'SRV("_s._tcp", 1, 2, 3, "foo.bar")',
    "alias-dotted-relative": 'R53_ALIAS("r", "A", "www.foo")',
    "mx-priority-word": 'MX("m", "x", "m.example.")',
    "mx-priority-overflow": 'MX("m", 70000, "m.example.")',
    "cname-at-apex": 'CNAME("@", "x.example.")',
    "cname-beside-a": 'CNAME("c", "x.example."), A("c", "192.0.2.2")',
    "two-cnames": 'CNAME("c1", "x.example."), CNAME("c1", "y.example.")',
    "exact-duplicate": 'A("d", "192.0.2.1"), A("d", "192.0.2.1")',
    "cname-loop": 'CNAME("loop", "loop")',
    "unknown-function": 'UNKNOWN_FN("x")',
    "bad-duration": 'A("t", "192.0.2.1", TTL("bogus"))',
    "caa-tag": 'CAA("@", "bogus", "x")',
    "syntax": 'A("x", "192.0.2.1"',
}


@pytest.mark.parametrize("body", INVALID.values(), ids=INVALID.keys())
def test_invalid_configuration_changes_nothing(project, zones, body):
    zone = zones("inv")
    zone.seed("keep", "A", 300, "192.0.2.50")
    project.config(f'D("{zone.name}", REG_NONE, DnsProvider(R53), A("n", "192.0.2.1"),\n  {body}\n);\n')
    before = zone.state()
    for command in ("check", "preview", "push"):
        failed(project.run(command))
        assert zone.state() == before, command


def test_zone_declared_twice_is_rejected(project, zones):
    zone = zones("dup")
    project.config(f"""D("{zone.name}", REG_NONE, DnsProvider(R53), A("a", "192.0.2.1"));
D("{zone.name}", REG_NONE, DnsProvider(R53), A("b", "192.0.2.2"));
""")
    failed(project.run("check"))
    failed(project.run("push"))
    assert zone.state() == zone.initial


def test_inconsistent_ttls_only_warn(project, zones):
    zone = zones("warn")
    project.config(f'D("{zone.name}", REG_NONE, DnsProvider(R53), A("t", "192.0.2.9", TTL(100)), A("t", "192.0.2.10", TTL(200)));\n')
    ok(project.run("check"))


def test_variables_includes_and_alternate_files(project, zones):
    zone = zones("req")
    z = zone.name
    project.write("lib/main.js", 'require("./inner/helper.js");\nfunction web(n, ip) { return [A(n, ip), TXT(n, "web " + ip)]; }\n')
    project.write("lib/inner/helper.js", 'var HELPER = "helper-loaded";\n')
    project.write("conf/site.js", HEADER + f"""require("../lib/main.js");
D("{z}", REG_NONE, DnsProvider(R53), DefaultTTL(ttl),
  web("www", ip),
  TXT("v", typeof ttl + ":" + ttl + ":" + (ttl + 1) + ":" + HELPER)
);
""")
    project.write("conf/other-creds.json", (project.root / "creds.json").read_text())
    (project.root / "creds.json").unlink()
    files = ("--config", "conf/site.js", "--creds", "conf/other-creds.json")

    ok(project.run("push", *files, "-v", "ip=192.0.2.44", "-v", "ttl=600"), 7)
    assert zone.state() == {
        **zone.apex(),
        ("www", "A"): rr(600, "192.0.2.44"),
        ("www", "TXT"): rr(600, '"web 192.0.2.44"'),
        ("v", "TXT"): rr(600, '"string:600:6001:helper-loaded"'),
    }
    ok(project.run("preview", *files, "-v", "ip=192.0.2.44", "-v", "ttl=600"), 0)
    ok(project.run("preview", *files, "--variable", "ip=192.0.2.45", "--variable", "ttl=600"), 2)

    # Without --creds the default creds.json (now absent) is used.
    failed(project.run("preview", "--config", "conf/site.js", "-v", "ip=192.0.2.45", "-v", "ttl=600"))


def test_missing_credentials_entry_changes_nothing(project, zones):
    good, other = zones("cr1"), zones("cr2")
    project.config(f"""D("{good.name}", REG_NONE, DnsProvider(R53), A("n", "192.0.2.1"));
D("{other.name}", REG_NONE, DnsProvider(NewDnsProvider("missing")), A("c", "192.0.2.2"));
""")
    failed(project.run("push"))
    assert good.state() == good.initial
    assert other.state() == other.initial


def test_expect_no_changes(project, zones):
    zone = zones("enc")
    z = zone.name
    zone.seed("www", "A", 300, "192.0.2.44")
    project.config(f'D("{z}", REG_NONE, DnsProvider(R53), A("www", "192.0.2.46"));\n')
    before = zone.state()
    result = failed(project.run("preview", "--expect-no-changes"))
    assert corrections(result) == 5
    assert zone.state() == before

    # push still applies the corrections, then reports that there were some.
    result = failed(project.run("push", "--expect-no-changes"))
    assert corrections(result) == 5
    assert zone.state() == {**zone.apex(), ("www", "A"): rr(300, "192.0.2.46")}
    ok(project.run("preview", "--expect-no-changes"), 0)
    ok(project.run("push", "--expect-no-changes"), 0)

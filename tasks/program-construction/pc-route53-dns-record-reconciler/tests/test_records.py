"""Single pushes into fresh zones: how each declared record becomes a Route 53 record set."""

from dnstest import alias, corrections, failed, ok, rr


def test_record_types_names_and_targets(project, zones):
    zone = zones("rec")
    zone.seed("old", "A", 60, "198.51.100.99")
    z = zone.name
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  A("@", "192.0.2.1"),
  A("WWW", "192.0.2.2"), A("www", "192.0.2.3"),
  A("*", "192.0.2.5"),
  A("deep.sub", "192.0.2.6"),
  AAAA("v6", "2001:DB8:0:0::1"),
  CNAME("c", "@"), CNAME("c2", "FOO.Example.COM."), CNAME("c3", "www"),
  MX("@", 10, "@"), MX("@", 20, "mx"), MX("m", 5, "Mail.Example.NET."),
  SRV("_sip._udp", 0, 5, 5060, "sip"), SRV("_sip._udp", 10, 0, 5061, "sip2.example.net."),
  CAA("@", "issue", "ca.example"), CAA("@", "issuewild", ";"), CAA("@", "iodef", "mailto:sec@example.net")
);
""")
    before = zone.state()
    # 4 apex NS TTL rewrites + 17 records + deletion of the unlisted seed.
    ok(project.run("preview"), 22)
    assert zone.state() == before

    ok(project.run("push"), 22)
    assert zone.state() == {
        **zone.apex(),
        ("@", "A"): rr(300, "192.0.2.1"),
        ("www", "A"): rr(300, "192.0.2.2", "192.0.2.3"),
        ("*", "A"): rr(300, "192.0.2.5"),
        ("deep.sub", "A"): rr(300, "192.0.2.6"),
        ("v6", "AAAA"): rr(300, "2001:db8::1"),
        ("c", "CNAME"): rr(300, f"{z}."),
        ("c2", "CNAME"): rr(300, "foo.example.com."),
        ("c3", "CNAME"): rr(300, f"www.{z}."),
        ("@", "MX"): rr(300, f"10 {z}.", f"20 mx.{z}."),
        ("m", "MX"): rr(300, "5 mail.example.net."),
        ("_sip._udp", "SRV"): rr(300, f"0 5 5060 sip.{z}.", "10 0 5061 sip2.example.net."),
        ("@", "CAA"): rr(300, '0 issue "ca.example"', '0 issuewild ";"', '0 iodef "mailto:sec@example.net"'),
    }
    ok(project.run("preview"), 0)
    ok(project.run("preview", "--expect-no-changes"), 0)


def test_txt_quoting_joining_and_chunking(project, zones):
    zone = zones("txt")
    z = zone.name
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  TXT("q", 'say "hi" \\\\ back'),
  TXT("m2", ["a b", "c"]),
  TXT("e", ""),
  TXT("semi", "v=spf1 ip4:192.0.2.0/24 -all; x"),
  TXT("ws", " padded "),
  TXT("l255", new Array(256).join("B")),
  TXT("l256", new Array(257).join("C")),
  TXT("l510", new Array(511).join("E")),
  TXT("multi", "one"), TXT("multi", "two")
);
""")
    ok(project.run("push"), 14)
    assert zone.state() == {
        **zone.apex(),
        ("q", "TXT"): rr(300, '"say \\"hi\\" \\\\ back"'),
        ("m2", "TXT"): rr(300, '"a bc"'),
        ("e", "TXT"): rr(300, '""'),
        ("semi", "TXT"): rr(300, '"v=spf1 ip4:192.0.2.0/24 -all; x"'),
        ("ws", "TXT"): rr(300, '" padded "'),
        ("l255", "TXT"): rr(300, '"' + "B" * 255 + '"'),
        ("l256", "TXT"): rr(300, '"' + "C" * 255 + '" "C"'),
        ("l510", "TXT"): rr(300, '"' + "E" * 255 + '" "' + "E" * 255 + '"'),
        ("multi", "TXT"): rr(300, '"one"', '"two"'),
    }
    ok(project.run("preview"), 0)


def test_ttl_defaults_and_durations(project, zones):
    zone = zones("ttl")
    z = zone.name
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  A("before", "192.0.2.1"),
  DefaultTTL("2h"),
  A("after", "192.0.2.2"),
  A("own", "192.0.2.3", TTL("5m")),
  A("week", "192.0.2.4", TTL("1w")),
  A("secs", "192.0.2.5", TTL("90s")),
  A("hour", "192.0.2.6", TTL("1h")),
  A("num", "192.0.2.7", TTL(86400)),
  DefaultTTL(900),
  A("later", "192.0.2.8")
);
""")
    ok(project.run("push"), 12)
    assert zone.state() == {
        **zone.apex(),
        ("before", "A"): rr(300, "192.0.2.1"),
        ("after", "A"): rr(7200, "192.0.2.2"),
        ("own", "A"): rr(300, "192.0.2.3"),
        ("week", "A"): rr(604800, "192.0.2.4"),
        ("secs", "A"): rr(90, "192.0.2.5"),
        ("hour", "A"): rr(3600, "192.0.2.6"),
        ("num", "A"): rr(86400, "192.0.2.7"),
        ("later", "A"): rr(900, "192.0.2.8"),
    }
    ok(project.run("preview"), 0)


def test_alias_record_sets(project, zones):
    zone = zones("ali")
    other = zones("ext")
    other.seed("lb", "A", 60, "198.51.100.50")
    z, e = zone.name, other.name
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  A("www", "192.0.2.2"), CNAME("c3", "www"), AAAA("v6", "2001:db8::5"),
  R53_ALIAS("al1", "A", "www", TTL(300), R53_EVALUATE_TARGET_HEALTH(false)),
  R53_ALIAS("al3", "CNAME", "c3", R53_EVALUATE_TARGET_HEALTH(false)),
  R53_ALIAS("al4", "A", "lb.{e}.", R53_ZONE("{other.id}")),
  R53_ALIAS("al5", "A", "www.{z}.", R53_EVALUATE_TARGET_HEALTH(true)),
  R53_ALIAS("@", "AAAA", "v6", R53_EVALUATE_TARGET_HEALTH(false))
);
""")
    other_before = other.state()
    ok(project.run("push"), 12)
    assert zone.state() == {
        **zone.apex(),
        ("www", "A"): rr(300, "192.0.2.2"),
        ("c3", "CNAME"): rr(300, f"www.{z}."),
        ("v6", "AAAA"): rr(300, "2001:db8::5"),
        ("al1", "A"): alias(f"www.{z}.", zone.id, False),
        ("al3", "CNAME"): alias(f"c3.{z}.", zone.id, False),
        ("al4", "A"): alias(f"lb.{e}.", other.id, False),
        ("al5", "A"): alias(f"www.{z}.", zone.id, True),
        ("@", "AAAA"): alias(f"v6.{z}.", zone.id, False),
    }
    assert other.state() == other_before
    ok(project.run("preview"), 0)


def test_alias_defaults_are_reproposed(project, zones):
    zone = zones("bare")
    z = zone.name
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53), DefaultTTL(600),
  A("www", "192.0.2.2", TTL(300)),
  R53_ALIAS("al", "A", "www"),
  R53_ALIAS("al2", "A", "www", R53_EVALUATE_TARGET_HEALTH(false)),
  R53_ALIAS("al3", "A", "www", TTL(300), R53_EVALUATE_TARGET_HEALTH(false))
);
""")
    expected = {
        **zone.apex(),
        ("www", "A"): rr(300, "192.0.2.2"),
        ("al", "A"): alias(f"www.{z}.", zone.id, False),
        ("al2", "A"): alias(f"www.{z}.", zone.id, False),
        ("al3", "A"): alias(f"www.{z}.", zone.id, False),
    }
    ok(project.run("push"), 8)
    assert zone.state() == expected
    # Stored aliases never match a declaration without the read-back TTL and health flag.
    ok(project.run("push"), 2)
    assert zone.state() == expected
    ok(project.run("preview"), 2)
    result = failed(project.run("preview", "--expect-no-changes"))
    assert corrections(result) == 2
    assert zone.state() == expected

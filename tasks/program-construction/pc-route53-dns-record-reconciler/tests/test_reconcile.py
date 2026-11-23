"""Reconciling declarations with existing zone content: updates, deletions, IGNORE, NO_PURGE, zone selection."""

from dnstest import alias, corrections, failed, ok, rr, unique_tag, zone_headers


def test_updates_deletions_and_type_changes(project, zones):
    # Adapted from the provider-neutral TTL / TypeChangeHard / testByRecordSet integration groups.
    zone = zones("mod")
    z = zone.name
    zone.seed("www", "A", 60, "203.0.113.1", "203.0.113.2")
    zone.seed("foo", "A", 300, "198.51.100.1")
    zone.seed("foo", "MX", 300, "10 mx.example.net.")
    zone.seed("x", "CNAME", 300, "target.example.net.")
    zone.seed("y", "A", 300, "198.51.100.9")
    zone.seed("ptr", "PTR", 300, "host.example.net.")
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  A("www", "203.0.113.1"), A("www", "203.0.113.3"),
  A("foo", "198.51.100.1"),
  A("x", "198.51.100.20"),
  CNAME("y", "x"),
  A("bar", "198.51.100.30")
);
""")
    before = zone.state()
    ok(project.run("preview"), 13)
    assert zone.state() == before
    ok(project.run("push"), 13)
    assert zone.state() == {
        **zone.apex(),
        ("www", "A"): rr(300, "203.0.113.1", "203.0.113.3"),
        ("foo", "A"): rr(300, "198.51.100.1"),
        ("x", "A"): rr(300, "198.51.100.20"),
        ("y", "CNAME"): rr(300, f"x.{z}."),
        ("bar", "A"): rr(300, "198.51.100.30"),
    }
    ok(project.run("preview"), 0)

    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  A("www", "203.0.113.1", TTL(120)),
  A("foo", "198.51.100.1"), MX("foo", 20, "mx2.example.net."),
  CNAME("x", "target2.example.net."),
  A("bar", "198.51.100.30")
);
""")
    ok(project.run("push"), 6)
    assert zone.state() == {
        **zone.apex(),
        ("www", "A"): rr(120, "203.0.113.1"),
        ("foo", "A"): rr(300, "198.51.100.1"),
        ("foo", "MX"): rr(300, "20 mx2.example.net."),
        ("x", "CNAME"): rr(300, "target2.example.net."),
        ("bar", "A"): rr(300, "198.51.100.30"),
    }
    ok(project.run("preview", "--expect-no-changes"), 0)


def test_equivalent_existing_values_are_left_alone(project, zones):
    zone = zones("eqv")
    z = zone.name
    zone.seed("up", "A", 300, "192.0.2.60")
    zone.seed("@", "MX", 300, "10 mx.example.net.")
    zone.seed("cn", "CNAME", 300, "target.example.net.")
    zone.seed("v6", "AAAA", 300, "2001:db8:0:0:0:0:0:1")
    zone.seed("cq", "CAA", 300, '0 issue "ca.example"')
    zone.seed("tx", "TXT", 300, "unquoted")
    zone.seed("txtj", "TXT", 300, '"a" "b"')
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  A("UP", "192.0.2.60"),
  MX("@", 10, "MX.Example.NET."),
  CNAME("cn", "TARGET.example.net."),
  AAAA("v6", "2001:db8::1"),
  CAA("cq", "issue", "ca.example"),
  TXT("tx", "unquoted"),
  TXT("txtj", "ab"),
  A("new", "192.0.2.1")
);
""")
    seeded = {k: v for k, v in zone.state().items() if k[1] not in ("NS", "SOA")}
    ok(project.run("push"), 5)
    assert zone.state() == {**zone.apex(), **seeded, ("new", "A"): rr(300, "192.0.2.1")}
    ok(project.run("preview"), 0)


def ignore_fixture(zone):
    zone.seed("foo", "A", 300, "1.2.3.4", "2.3.4.5")
    zone.seed("foo", "TXT", 300, '"simple"')
    zone.seed("bar", "A", 300, "5.4.5.4")
    zone.seed("mail", "CNAME", 300, "ghs.googlehosted.com.")
    zone.seed("lab.bat", "A", 300, "9.9.9.1")
    zone.seed("lab2.bat", "TXT", 300, '"x"')
    zone.seed("@", "TXT", 300, '"apex"')


def test_ignore_one_member_of_a_record_set(project, zones):
    # Adapted from the "IGNORE main" / "IGNORE with modify" integration groups.
    zone = zones("ign")
    z = zone.name
    ignore_fixture(zone)
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  A("foo", "9.9.9.9"), TXT("foo", "simple"),
  A("bar", "5.4.5.4"), CNAME("mail", "ghs.googlehosted.com."),
  IGNORE("foo", "A", "1.2.3.4"), IGNORE_NAME("lab*")
);
""")
    before = zone.state()
    ok(project.run("preview"), 8)
    assert zone.state() == before
    ok(project.run("push"), 8)
    assert zone.state() == {
        **zone.apex(),
        ("foo", "A"): rr(300, "1.2.3.4", "9.9.9.9"),
        ("foo", "TXT"): rr(300, '"simple"'),
        ("bar", "A"): rr(300, "5.4.5.4"),
        ("mail", "CNAME"): rr(300, "ghs.googlehosted.com."),
    }
    ok(project.run("preview"), 0)


def test_ignore_pattern_forms(project, zones):
    zone = zones("igp")
    z = zone.name
    zone.seed("lab.bat", "A", 300, "9.9.9.1")
    zone.seed("lab.bat", "TXT", 300, '"t"')
    zone.seed("lab.bat", "MX", 300, "10 m.example.net.")
    zone.seed("x", "A", 300, "9.9.9.2")
    zone.seed("y", "A", 300, "9.9.9.3")
    zone.seed("z", "A", 300, "9.9.9.4")
    zone.seed("q", "A", 300, "9.9.9.5")
    zone.seed("q", "TXT", 300, '"q"')
    zone.seed("cn", "CNAME", 300, "thing.cdn.example.net.")
    zone.seed("cn2", "CNAME", 300, "other.example.org.")
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  A("new", "192.0.2.1"),
  IGNORE("lab.b?t", "A,TXT"),
  IGNORE_NAME("{{x,y}}"),
  IGNORE_NAME("q", "TXT"),
  IGNORE("cn*", "CNAME", "*.cdn.example.net.")
);
""")
    ok(project.run("push"), 9)
    assert zone.state() == {
        **zone.apex(),
        ("new", "A"): rr(300, "192.0.2.1"),
        ("lab.bat", "A"): rr(300, "9.9.9.1"),
        ("lab.bat", "TXT"): rr(300, '"t"'),
        ("x", "A"): rr(300, "9.9.9.2"),
        ("y", "A"): rr(300, "9.9.9.3"),
        ("q", "TXT"): rr(300, '"q"'),
        ("cn", "CNAME"): rr(300, "thing.cdn.example.net."),
    }
    ok(project.run("preview"), 0)


def test_declaring_an_ignored_record_is_refused(project, zones):
    # Adapted from the "IGNORE unsafe" integration group.
    zone = zones("uns")
    z = zone.name
    zone.seed("bar", "A", 300, "5.4.5.4")
    zone.seed("mail", "CNAME", 300, "ghs.googlehosted.com.")
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  A("bar", "5.4.5.5"), A("new", "192.0.2.1"),
  IGNORE_NAME("bar"), IGNORE_NAME("mail")
);
""")
    before = zone.state()
    assert project.run("check").returncode == 0
    failed(project.run("preview"))
    assert zone.state() == before
    failed(project.run("push"))
    assert zone.state() == before


def test_ignoring_the_apex_conflicts_with_its_nameservers(project, zones):
    zone = zones("iga")
    z = zone.name
    ignore_fixture(zone)
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  A("bar", "5.4.5.4"), CNAME("mail", "ghs.googlehosted.com."),
  IGNORE_NAME("foo"), IGNORE("*.bat", "A"), IGNORE_NAME("@")
);
""")
    before = zone.state()
    failed(project.run("preview"))
    failed(project.run("push"))
    assert zone.state() == before


def test_ignore_without_conflict_keeps_matching_sets(project, zones):
    zone = zones("igk")
    z = zone.name
    ignore_fixture(zone)
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  A("bar", "5.4.5.4"),
  IGNORE_NAME("foo"), IGNORE("*.bat", "A"), IGNORE("", "", "*.googlehosted.com.")
);
""")
    ok(project.run("push"), 6)
    assert zone.state() == {
        **zone.apex(),
        ("foo", "A"): rr(300, "1.2.3.4", "2.3.4.5"),
        ("foo", "TXT"): rr(300, '"simple"'),
        ("bar", "A"): rr(300, "5.4.5.4"),
        ("mail", "CNAME"): rr(300, "ghs.googlehosted.com."),
        ("lab.bat", "A"): rr(300, "9.9.9.1"),
    }
    ok(project.run("preview"), 0)


def test_no_purge(project, zones):
    zone = zones("np")
    z = zone.name
    zone.seed("legacy", "A", 120, "198.51.100.1")
    zone.seed("www", "A", 60, "203.0.113.9", "203.0.113.10")
    zone.seed("txt", "TXT", 120, '"old"')
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53), NO_PURGE,
  A("www", "203.0.113.9"), TXT("txt", "new"), A("new", "198.51.100.7")
);
""")
    ok(project.run("push"), 8)
    assert zone.state() == {
        **zone.apex(),
        ("legacy", "A"): rr(120, "198.51.100.1"),
        ("www", "A"): rr(300, "203.0.113.9"),
        ("txt", "TXT"): rr(300, '"new"'),
        ("new", "A"): rr(300, "198.51.100.7"),
    }
    ok(project.run("preview"), 0)


def test_domains_selects_zones(project, zones):
    tag = unique_tag()
    one, two, three = zones(name=f"one.{tag}.example"), zones(name=f"two.{tag}.example"), zones(name=f"three-{tag}.test")
    project.config(f"""D("{one.name}", REG_NONE, DnsProvider(R53), A("n", "192.0.2.1"));
D("{two.name}", REG_NONE, DnsProvider(R53), A("n", "192.0.2.2"));
D("{three.name}", REG_NONE, DnsProvider(R53), A("n", "192.0.2.3"));
""")
    initial = {zone.name: zone.state() for zone in (one, two, three)}

    result = ok(project.run("preview", "--domains", f"*.{tag}.example"), 10)
    assert zone_headers(result) == {one.name: 1, two.name: 1}
    result = ok(project.run("preview", "--domains", three.name), 5)
    assert zone_headers(result) == {three.name: 1}
    assert {zone.name: zone.state() for zone in (one, two, three)} == initial

    result = ok(project.run("push", "--domains", f"{two.name},{three.name}"), 10)
    assert zone_headers(result) == {two.name: 1, three.name: 1}
    assert one.state() == initial[one.name]
    for zone, address in ((two, "192.0.2.2"), (three, "192.0.2.3")):
        assert zone.state() == {**zone.apex(), ("n", "A"): rr(300, address)}

    result = ok(project.run("preview", "--domains", f"absent-{tag}.example"), 0)
    assert zone_headers(result) == {}
    result = failed(project.run("preview", "--domains", one.name, "--expect-no-changes"))
    assert corrections(result) == 5
    ok(project.run("push", "--domains", two.name, "--expect-no-changes"), 0)
    result = ok(project.run("preview"), 5)
    assert zone_headers(result) == {one.name: 1, two.name: 1, three.name: 1}
    assert one.state() == initial[one.name]


def test_validation_error_in_one_zone_blocks_all(project, zones):
    good, bad = zones("vok"), zones("vbad")
    good.seed("old", "A", 300, "192.0.2.50")
    project.config(f"""D("{good.name}", REG_NONE, DnsProvider(R53), A("n", "192.0.2.1"));
D("{bad.name}", REG_NONE, DnsProvider(R53), CNAME("c", "x.example."), A("c", "192.0.2.2"));
""")
    before = (good.state(), bad.state())
    failed(project.run("push"))
    assert (good.state(), bad.state()) == before


def test_unsafe_zone_does_not_block_other_zones(project, zones):
    first, unsafe, last = zones("ua"), zones("ub"), zones("uc")
    first.seed("old", "A", 300, "192.0.2.50")
    project.config(f"""D("{first.name}", REG_NONE, DnsProvider(R53), A("n", "192.0.2.1"));
D("{unsafe.name}", REG_NONE, DnsProvider(R53), A("c", "192.0.2.2"), A("d", "192.0.2.3"), IGNORE_NAME("c"));
D("{last.name}", REG_NONE, DnsProvider(R53), A("n", "192.0.2.4"));
""")
    unsafe_before = unsafe.state()
    result = failed(project.run("push"))
    assert corrections(result) == 11
    assert first.state() == {**first.apex(), ("n", "A"): rr(300, "192.0.2.1")}
    assert unsafe.state() == unsafe_before
    assert last.state() == {**last.apex(), ("n", "A"): rr(300, "192.0.2.4")}


def test_alias_and_plain_record_conversions(project, zones):
    # Adapted from the R53_ALIAS2 / R53_B3493 / R53_B3493_REV / evaluate_target_health groups.
    zone = zones("conv")
    z = zone.name
    zone.seed("dev", "CNAME", 300, f"dev18.{z}.")
    zone.seed("dev18", "CNAME", 300, "ec2-54-91-33-155.compute-1.amazonaws.com.")
    zone.seed("web", "A", 300, "192.0.2.7")
    zone.seed("tgt", "A", 300, "192.0.2.8")
    plain = {
        ("dev18", "CNAME"): rr(300, "ec2-54-91-33-155.compute-1.amazonaws.com."),
        ("tgt", "A"): rr(300, "192.0.2.8"),
    }
    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  R53_ALIAS("dev", "CNAME", "dev18", R53_EVALUATE_TARGET_HEALTH(false)),
  CNAME("dev18", "ec2-54-91-33-155.compute-1.amazonaws.com."),
  R53_ALIAS("web", "A", "tgt", R53_EVALUATE_TARGET_HEALTH(false)), A("tgt", "192.0.2.8")
);
""")
    ok(project.run("push"), 8)
    assert zone.state() == {
        **zone.apex(),
        **plain,
        ("dev", "CNAME"): alias(f"dev18.{z}.", zone.id, False),
        ("web", "A"): alias(f"tgt.{z}.", zone.id, False),
    }
    ok(project.run("preview"), 0)

    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  CNAME("dev", "dev18"),
  CNAME("dev18", "ec2-54-91-33-155.compute-1.amazonaws.com."),
  R53_ALIAS("web", "A", "tgt", R53_EVALUATE_TARGET_HEALTH(true)), A("tgt", "192.0.2.8"),
  R53_ALIAS("@", "A", "tgt", TTL(600), R53_EVALUATE_TARGET_HEALTH(false))
);
""")
    ok(project.run("push"), 4)
    after_second = {
        **zone.apex(),
        **plain,
        ("dev", "CNAME"): rr(300, f"dev18.{z}."),
        ("web", "A"): alias(f"tgt.{z}.", zone.id, True),
        ("@", "A"): alias(f"tgt.{z}.", zone.id, False),
    }
    assert zone.state() == after_second
    # The apex alias declares TTL 600, which an alias set never reports back.
    ok(project.run("preview"), 1)
    assert zone.state() == after_second

    project.config(f"""D("{z}", REG_NONE, DnsProvider(R53),
  CNAME("dev", "dev18"),
  CNAME("dev18", "ec2-54-91-33-155.compute-1.amazonaws.com."),
  A("web", "192.0.2.77"), A("tgt", "192.0.2.8")
);
""")
    ok(project.run("push"), 3)
    assert zone.state() == {
        **zone.apex(),
        **plain,
        ("dev", "CNAME"): rr(300, f"dev18.{z}."),
        ("web", "A"): rr(300, "192.0.2.77"),
    }
    ok(project.run("preview"), 0)

"""Temporary credentials: GetSessionToken vs AssumeRole, durations, the session cache and clear."""

from conftest import assert_lifetime, assert_sessions, identity

GST = "sts.GetSessionToken"


def same_session(a, b):
    return (a["AccessKeyId"], a["SecretAccessKey"], a["SessionToken"], a["Expiration"]) == (b["AccessKeyId"], b["SecretAccessKey"], b["SessionToken"], b["Expiration"])


def role_config(base_extra="", **roles):
    text = f"[profile base]\n{base_extra}\n"
    for name, body in roles.items():
        text += f"[profile {name}]\n{body}\n"
    return text


def test_plain_profile_session_is_cached_and_shared(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    vault.add("base", user)
    first = vault.export("base")
    assert_lifetime(first["Expiration"], 3600, first["_window"])
    assert identity(first) == user.arn
    assert_sessions(vault.sessions(), [(GST, 3600)])
    second = vault.export("base")
    assert same_session(first, second)
    child = vault.exec_env("base")
    assert (child["AWS_ACCESS_KEY_ID"], child["AWS_SESSION_TOKEN"], child["AWS_CREDENTIAL_EXPIRATION"]) == (first["AccessKeyId"], first["SessionToken"], first["Expiration"])
    assert_sessions(vault.sessions(), [(GST, 3600)])
    assert aws.keys(user) == [user.key_id]


def test_cached_session_wins_over_a_new_duration_until_cleared(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    vault.add("base", user)
    short = vault.export("base", "-d", "15m")
    assert_lifetime(short["Expiration"], 900, short["_window"])
    assert_sessions(vault.sessions(), [(GST, 900)])
    assert same_session(short, vault.export("base", "-d", "2h"))
    assert vault.ok("clear", "base").stdout == "Cleared 1 sessions.\n"
    assert vault.sessions() == []
    longer = vault.export("base", "-d", "2h")
    assert longer["AccessKeyId"] != short["AccessKeyId"]
    assert_lifetime(longer["Expiration"], 7200, longer["_window"])
    assert_sessions(vault.sessions(), [(GST, 7200)])


def test_session_duration_sources(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    vault.add("base", user)
    cases = [
        ([], {"AWS_SESSION_TOKEN_TTL": "20m"}, 1200),
        (["-d", "30m"], {"AWS_SESSION_TOKEN_TTL": "20m"}, 1800),
        ([], {"AWS_VAULT_DURATION": "25m"}, 1500),
        (["--duration=40m"], {"AWS_VAULT_DURATION": "25m"}, 2400),
    ]
    for flags, env, seconds in cases:
        creds = vault.export("base", *flags, env=env)
        assert_lifetime(creds["Expiration"], seconds, creds["_window"])
        assert_sessions(vault.sessions(), [(GST, seconds)])
        vault.ok("clear")


def test_min_ttl_refreshes_a_session_that_expires_too_soon(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    vault.add("base", user)
    short = vault.export("base", "-d", "15m")
    assert same_session(short, vault.export("base", env={"AWS_MIN_TTL": "10m"}))
    fresh = vault.export("base", env={"AWS_MIN_TTL": "20m"})
    assert fresh["AccessKeyId"] != short["AccessKeyId"]
    assert_lifetime(fresh["Expiration"], 3600, fresh["_window"])
    # The refreshed session replaces the old cache entry rather than adding one.
    assert_sessions(vault.sessions(), [(GST, 3600)])
    assert same_session(fresh, vault.export("base"))
    assert identity(fresh) == user.arn


def test_role_credentials_are_assumed_each_time_and_not_cached(vault, aws):
    user, role = aws.user(), aws.role()
    vault.write_config(role_config(ops=f"source_profile = base\nrole_arn = {role.arn}\nrole_session_name = ops-session\n"))
    vault.add("base", user)
    first, second = vault.export("ops"), vault.export("ops")
    assert first["AccessKeyId"] != second["AccessKeyId"]
    for creds in (first, second):
        assert identity(creds) == f"arn:aws:sts::000000000000:assumed-role/{role.name}/ops-session"
        assert_lifetime(creds["Expiration"], 3600, creds["_window"])
    # The role was assumed straight from the stored key: no session for base either.
    assert vault.sessions() == []
    assert vault.export("ops", "-n")["AccessKeyId"] not in (first["AccessKeyId"], second["AccessKeyId"])
    assert vault.sessions() == []


def test_role_duration_sources(vault, aws):
    user, role = aws.user(), aws.role()
    vault.write_config(role_config(
        ops=f"source_profile = base\nrole_arn = {role.arn}\n",
        long=f"source_profile = base\nrole_arn = {role.arn}\nduration_seconds = 7200\n",
    ))
    vault.add("base", user)
    cases = [
        ("ops", ["-d", "2h"], {}, 7200),
        ("ops", [], {"AWS_ASSUME_ROLE_TTL": "3h"}, 10800),
        ("long", [], {}, 7200),
        ("long", [], {"AWS_ASSUME_ROLE_TTL": "20m"}, 1200),
        ("long", ["-d", "30m"], {"AWS_ASSUME_ROLE_TTL": "20m"}, 1800),
        ("long", [], {"AWS_VAULT_DURATION": "25m"}, 1500),
    ]
    for profile, flags, env, seconds in cases:
        creds = vault.export(profile, *flags, env=env)
        assert_lifetime(creds["Expiration"], seconds, creds["_window"])
        assert f":assumed-role/{role.name}/" in identity(creds)
    assert vault.sessions() == []


def test_role_chain_is_capped_to_one_hour(vault, aws):
    user, first, second = aws.user(), aws.role(), aws.role()
    vault.write_config(role_config(
        ops=f"source_profile = base\nrole_arn = {first.arn}\n",
        deep=f"source_profile = ops\nrole_arn = {second.arn}\n",
        deeplong=f"source_profile = ops\nrole_arn = {second.arn}\nduration_seconds = 10800\n",
    ))
    vault.add("base", user)
    single = vault.export("ops", "-d", "2h")
    assert_lifetime(single["Expiration"], 7200, single["_window"])
    for profile, flags in (("deep", ["-d", "2h"]), ("deeplong", []), ("deep", [])):
        creds = vault.export(profile, *flags)
        assert_lifetime(creds["Expiration"], 3600, creds["_window"])
        assert f":assumed-role/{second.name}/" in identity(creds)
    short = vault.export("deep", "-d", "20m")
    assert_lifetime(short["Expiration"], 1200, short["_window"])
    assert vault.sessions() == []


def test_role_profile_with_its_own_stored_key(vault, aws):
    user, role = aws.user(), aws.role()
    vault.write_config(f"[profile direct]\nrole_arn = {role.arn}\nrole_session_name = direct-session\n")
    vault.add("direct", user)
    arn = f"arn:aws:sts::000000000000:assumed-role/{role.name}/direct-session"
    plain = vault.export("direct")
    assert plain["AccountId"] == "000000000000"
    assert identity(plain) == arn
    assert_lifetime(plain["Expiration"], 3600, plain["_window"])
    longer = vault.export("direct", "-d", "3h")
    assert_lifetime(longer["Expiration"], 10800, longer["_window"])
    nosession = vault.export("direct", "--no-session")
    assert nosession["AccessKeyId"] != user.key_id and nosession.get("SessionToken")
    assert identity(nosession) == arn
    assert vault.sessions() == []


def test_alias_profile_gets_its_own_cached_session(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n\n[profile alias]\nsource_profile = base\n")
    vault.add("base", user)
    alias = vault.export("alias")
    assert alias["AccessKeyId"] != user.key_id
    assert identity(alias) == user.arn
    assert "AccountId" not in alias
    assert_sessions(vault.sessions(), [(GST, 3600)])
    base = vault.export("base")
    assert base["AccessKeyId"] != alias["AccessKeyId"]
    assert_sessions(vault.sessions(), [(GST, 3600), (GST, 3600)])
    assert same_session(alias, vault.export("alias"))
    master = vault.export("alias", "-n")
    assert (master["AccessKeyId"], master["SecretAccessKey"]) == (user.key_id, user.secret)
    assert vault.ok("clear", "alias").stdout == "Cleared 1 sessions.\n"
    assert same_session(base, vault.export("base"))
    renewed = vault.export("alias")
    assert renewed["AccessKeyId"] not in (alias["AccessKeyId"], base["AccessKeyId"])


def test_clear_counts_and_scope(vault, aws):
    user, other = aws.user(), aws.user()
    vault.write_config("[profile base]\n\n[profile alias]\nsource_profile = base\n\n[profile other]\n")
    vault.add("base", user)
    vault.add("other", other)
    sessions = {name: vault.export(name) for name in ("base", "alias", "other")}
    assert len(vault.sessions()) == 3
    assert vault.ok("clear", "nosuch").stdout == "Cleared 0 sessions.\n"
    assert len(vault.sessions()) == 3
    assert vault.ok("clear", "other").stdout == "Cleared 1 sessions.\n"
    assert len(vault.sessions()) == 2
    assert same_session(sessions["base"], vault.export("base"))
    assert same_session(sessions["alias"], vault.export("alias"))
    assert vault.ok("clear").stdout == "Cleared 2 sessions.\n"
    assert vault.ok("clear").stdout == "Cleared 0 sessions.\n"
    assert vault.sessions() == []
    assert sorted(vault.credentials()) == ["base", "other"]
    assert vault.export("base")["AccessKeyId"] != sessions["base"]["AccessKeyId"]


def test_failed_sts_call_caches_nothing(vault, aws):
    user, role = aws.user(), aws.role()
    vault.write_config(role_config(ops=f"source_profile = base\nrole_arn = {role.arn}\n"))
    vault.add("base", user)
    unreachable = {"AWS_ENDPOINT_URL": "http://127.0.0.1:9"}
    for profile in ("base", "ops"):
        result = vault.run("export", "--format=json", profile, env=unreachable)
        assert result.returncode != 0 and result.stdout == ""
    assert vault.sessions() == []
    # The stored key itself needs no AWS call.
    assert vault.export("base", "-n", env=unreachable)["AccessKeyId"] == user.key_id
    assert vault.sessions() == []
    assert identity(vault.export("base")) == user.arn

"""Invocation and serialization: export formats, list views and refusal diagnostics."""

from conftest import ACCOUNT, assert_lifetime, expiry, identity, parse_env, parse_ini, parse_table

NO_REGION = {"AWS_REGION": "", "AWS_DEFAULT_REGION": ""}


def test_export_json_session_shape(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\nregion = us-west-2\n")
    vault.add("base", user)
    creds = vault.export("base")
    assert set(creds) - {"_window"} == {"Version", "AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration"}
    assert creds["AccessKeyId"] != user.key_id and creds["SecretAccessKey"] != user.secret
    assert_lifetime(creds["Expiration"], 3600, creds["_window"])
    assert identity(creds) == user.arn


def test_export_formats_render_the_same_session(vault, aws):
    """env, export-env and ini print the cached session that json printed, plus the region."""
    user = aws.user()
    vault.write_config("[profile base]\nregion = us-west-2\n")
    vault.add("base", user)
    creds = vault.export("base", env=NO_REGION)
    expected_env = {
        "AWS_ACCESS_KEY_ID": creds["AccessKeyId"],
        "AWS_SECRET_ACCESS_KEY": creds["SecretAccessKey"],
        "AWS_SESSION_TOKEN": creds["SessionToken"],
        "AWS_CREDENTIAL_EXPIRATION": creds["Expiration"],
        "AWS_REGION": "us-west-2",
        "AWS_DEFAULT_REGION": "us-west-2",
    }
    default = vault.ok("export", "base", env=NO_REGION)
    assert parse_env(default.stdout) == expected_env
    assert parse_env(vault.ok("export", "--format=env", "base", env=NO_REGION).stdout) == expected_env
    assert parse_env(vault.ok("export", "--format=export-env", "base", env=NO_REGION).stdout, prefix="export ") == expected_env
    section, values = parse_ini(vault.ok("export", "--format=ini", "base", env=NO_REGION).stdout)
    assert section == "base"
    assert values == {
        "aws_access_key_id": creds["AccessKeyId"],
        "aws_secret_access_key": creds["SecretAccessKey"],
        "aws_session_token": creds["SessionToken"],
        "aws_credential_expiration": creds["Expiration"],
        "region": "us-west-2",
    }


def test_export_no_session_prints_stored_key_only(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\nregion = eu-central-1\n")
    vault.add("base", user)
    creds = vault.export("base", "--no-session")
    assert {k: v for k, v in creds.items() if k != "_window"} == {"Version": 1, "AccessKeyId": user.key_id, "SecretAccessKey": user.secret}
    assert parse_env(vault.ok("export", "-n", "base", env=NO_REGION).stdout) == {
        "AWS_ACCESS_KEY_ID": user.key_id,
        "AWS_SECRET_ACCESS_KEY": user.secret,
        "AWS_REGION": "eu-central-1",
        "AWS_DEFAULT_REGION": "eu-central-1",
    }
    section, values = parse_ini(vault.ok("export", "-n", "--format=ini", "base", env=NO_REGION).stdout)
    assert (section, values) == ("base", {"aws_access_key_id": user.key_id, "aws_secret_access_key": user.secret, "region": "eu-central-1"})
    # --no-session never creates a cached session.
    assert vault.sessions() == []


def test_export_role_credentials_carry_account(vault, aws):
    user, role = aws.user(), aws.role()
    vault.write_config(f"[profile base]\n\n[profile ops]\nsource_profile = base\nrole_arn = {role.arn}\nregion = ap-southeast-2\n")
    vault.add("base", user)
    creds = vault.export("ops")
    assert set(creds) - {"_window"} == {"Version", "AccessKeyId", "SecretAccessKey", "SessionToken", "Expiration", "AccountId"}
    assert creds["AccountId"] == ACCOUNT
    assert f":assumed-role/{role.name}/" in identity(creds)
    env = parse_env(vault.ok("export", "--format=env", "ops", env=NO_REGION).stdout)
    assert set(env) == {"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_CREDENTIAL_EXPIRATION", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ACCOUNT_ID"}
    assert (env["AWS_ACCOUNT_ID"], env["AWS_REGION"], env["AWS_DEFAULT_REGION"]) == (ACCOUNT, "ap-southeast-2", "ap-southeast-2")
    expiry(env["AWS_CREDENTIAL_EXPIRATION"])
    section, values = parse_ini(vault.ok("export", "--format=ini", "ops", env=NO_REGION).stdout)
    assert section == "ops"
    assert set(values) == {"aws_access_key_id", "aws_secret_access_key", "aws_session_token", "aws_credential_expiration", "region", "aws_account_id"}
    assert (values["aws_account_id"], values["region"]) == (ACCOUNT, "ap-southeast-2")


def test_unknown_profile_is_refused_everywhere(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    vault.add("base", user)
    marker = vault.root / "ran"
    for args in (["export", "nosuch"], ["exec", "nosuch", "--", "touch", str(marker)], ["rotate", "-n", "nosuch"]):
        result = vault.run(*args)
        assert result.returncode != 0
        assert result.stdout == ""
        assert "profile 'nosuch' not found" in result.stderr
    assert not marker.exists()
    assert vault.sessions() == []
    assert aws.keys(user) == [user.key_id]


def test_refuses_to_run_inside_an_exec_environment(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    vault.add("base", user)
    marker = vault.root / "ran"
    for args in (["export", "--format=json", "base"], ["exec", "base", "--", "touch", str(marker)]):
        result = vault.run(*args, env={"AWS_VAULT": "outer"})
        assert result.returncode != 0
        assert result.stdout == ""
        assert "existing aws-vault subshell" in result.stderr
    assert not marker.exists()
    assert vault.sessions() == []


def test_invalid_flag_values_fail_before_any_session(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    vault.add("base", user)
    for args in (["export", "--format=yaml", "base"], ["export", "-d", "soon", "base"], ["exec", "-d", "soon", "base", "--", "true"]):
        result = vault.run(*args)
        assert result.returncode != 0
        assert result.stdout == ""
    assert vault.sessions() == []


def test_list_views(vault, aws):
    user = aws.user()
    vault.write_config("[default]\nregion = us-east-2\n\n[profile zeta]\nregion = eu-west-3\n\n[profile alpha]\n\n[profile mid]\nsource_profile = zeta\n")
    vault.add("zeta", user)
    vault.add("orphan", user, "--no-add-config")
    vault.export("zeta")
    assert vault.ok("list", "--profiles").stdout.splitlines() == ["default", "zeta", "alpha", "mid"]
    assert sorted(vault.credentials()) == ["orphan", "zeta"]
    for command in ("list", "ls"):
        rows = parse_table(vault.ok(command).stdout)
        assert [row[:2] for row in rows] == [["default", "-"], ["zeta", "zeta"], ["alpha", "-"], ["mid", "-"], ["-", "orphan"]]
        assert [row[2] for row in rows if row[0] != "zeta"] == ["-"] * 4
        zeta = next(row for row in rows if row[0] == "zeta")[2]
        assert zeta.startswith("sts.GetSessionToken:")


def test_list_empty_store(vault):
    vault.write_config("[profile solo]\n")
    assert vault.ok("list", "--credentials").stdout == ""
    assert vault.ok("list", "--sessions").stdout == ""
    assert parse_table(vault.ok("list").stdout) == [["solo", "-", "-"]]


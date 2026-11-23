"""Profile resolution from the config file: region, endpoint, account id, inclusion and loops."""

from conftest import ACCOUNT, assert_lifetime, identity, parse_env
from harness import write_fixture

NO_REGION = {"AWS_REGION": "", "AWS_DEFAULT_REGION": ""}


def region_of(vault, profile, *flags, env=None):
    pairs = parse_env(vault.ok("export", "-n", *flags, profile, env=env).stdout)
    assert pairs.get("AWS_REGION") == pairs.get("AWS_DEFAULT_REGION"), pairs
    return pairs.get("AWS_REGION")


def test_region_precedence(vault, aws):
    user = aws.user()
    vault.write_config("[default]\nregion = sa-east-1\n\n[profile base]\nregion = us-west-2\n")
    vault.add("base", user)
    assert region_of(vault, "base", env=NO_REGION) == "us-west-2"
    assert region_of(vault, "base", env={"AWS_REGION": "", "AWS_DEFAULT_REGION": "ca-central-1"}) == "ca-central-1"
    assert region_of(vault, "base", env={"AWS_REGION": "ap-south-1", "AWS_DEFAULT_REGION": "ca-central-1"}) == "ap-south-1"
    assert region_of(vault, "base", "--region=me-south-1", env={"AWS_REGION": "ap-south-1"}) == "me-south-1"
    child = vault.exec_env("base", "-n", "--region", "eu-south-1", env=NO_REGION)
    assert (child["AWS_REGION"], child["AWS_DEFAULT_REGION"]) == ("eu-south-1", "eu-south-1")


def test_region_comes_from_default_section_not_source_profile(vault, aws):
    user, role = aws.user(), aws.role()
    vault.write_config(
        "[default]\nregion = sa-east-1\n\n[profile base]\nregion = us-west-2\n\n"
        f"[profile alias]\nsource_profile = base\n\n[profile ops]\nsource_profile = base\nrole_arn = {role.arn}\n"
    )
    vault.add("base", user)
    vault.add("loose", user, "--no-add-config")
    assert region_of(vault, "alias", env=NO_REGION) == "sa-east-1"
    assert region_of(vault, "loose", env=NO_REGION) == "sa-east-1"
    pairs = parse_env(vault.ok("export", "ops", env=NO_REGION).stdout)
    assert (pairs["AWS_REGION"], pairs["AWS_DEFAULT_REGION"]) == ("sa-east-1", "sa-east-1")
    child = vault.exec_env("ops", env=NO_REGION)
    assert (child["AWS_REGION"], child["AWS_DEFAULT_REGION"]) == ("sa-east-1", "sa-east-1")


def test_no_region_anywhere_prints_none(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n\n[profile alias]\nsource_profile = base\n")
    vault.add("base", user)
    for profile in ("base", "alias"):
        pairs = parse_env(vault.ok("export", "-n", profile, env=NO_REGION).stdout)
        assert pairs == {"AWS_ACCESS_KEY_ID": user.key_id, "AWS_SECRET_ACCESS_KEY": user.secret}


def test_endpoint_sources_and_precedence(vault, aws):
    user = aws.user()
    vault.write_config(
        "[default]\nendpoint_url = http://engines:4566/\n\n"
        "[profile base]\nendpoint_url = http://engines:4566\n\n"
        "[profile plain]\n\n"
        "[profile far]\nendpoint_url = http://127.0.0.1:9\n"
    )
    for name in ("base", "plain", "far"):
        vault.add(name, user)
    no_env = {"AWS_ENDPOINT_URL": ""}
    assert vault.exec_env("base", env=no_env)["AWS_ENDPOINT_URL"] == "http://engines:4566"
    assert vault.exec_env("plain", env=no_env)["AWS_ENDPOINT_URL"] == "http://engines:4566/"
    refused = vault.run("export", "far", env=no_env)
    assert refused.returncode != 0 and refused.stdout == ""
    assert len(vault.sessions()) == 2
    # The environment variable outranks every endpoint_url key.
    far = vault.exec_env("far", env={"AWS_ENDPOINT_URL": "http://engines:4566"})
    assert far["AWS_ENDPOINT_URL"] == "http://engines:4566"
    assert identity(far) == user.arn
    vault.ok("clear", "base")
    blocked = vault.run("export", "base", env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9"})
    assert blocked.returncode != 0 and blocked.stdout == ""
    assert len(vault.sessions()) == 2
    master = vault.exec_env("plain", "-n", env={"AWS_ENDPOINT_URL": "http://127.0.0.1:9"})
    assert master["AWS_ENDPOINT_URL"] == "http://127.0.0.1:9" and master["AWS_ACCESS_KEY_ID"] == user.key_id


def test_account_id_resolution(vault, aws):
    user, role = aws.user(), aws.role()
    vault.write_config(
        "[default]\naws_account_id = 777777777777\n\n"
        "[profile base]\naws_account_id = 555555555555\n\n"
        "[profile plain]\n\n"
        "[profile alias]\nsource_profile = base\naws_account_id = 666666666666\n\n"
        "[profile alias2]\nsource_profile = plain\naws_account_id = 666666666666\n\n"
        f"[profile ops]\nsource_profile = base\nrole_arn = {role.arn}\naws_account_id = 999999999999\n"
    )
    vault.add("base", user)
    vault.add("plain", user)
    expected = {"base": "555555555555", "plain": None, "alias": "555555555555", "alias2": "666666666666"}
    for profile, account in expected.items():
        assert parse_env(vault.ok("export", "-n", profile).stdout).get("AWS_ACCOUNT_ID") == account, profile
        exported = vault.export(profile)
        assert exported.get("AccountId") == account, profile
    assert vault.export("ops")["AccountId"] == ACCOUNT
    assert vault.exec_env("ops")["AWS_ACCOUNT_ID"] == ACCOUNT
    assert "AWS_ACCOUNT_ID" not in vault.exec_env("plain")


def test_include_profile_inherits_role_settings(vault, aws):
    user, role = aws.user(), aws.role()
    vault.write_config(
        "[profile base]\nregion = us-west-2\n\n"
        f"[profile shared]\nrole_arn = {role.arn}\nregion = ap-northeast-1\nduration_seconds = 7200\nrole_session_name = shared-session\n\n"
        "[profile inc]\ninclude_profile = shared\nsource_profile = base\n\n"
        "[profile local]\ninclude_profile = shared\nsource_profile = base\nregion = eu-west-2\n"
    )
    vault.add("base", user)
    creds = vault.export("inc")
    assert identity(creds) == f"arn:aws:sts::{ACCOUNT}:assumed-role/{role.name}/shared-session"
    assert_lifetime(creds["Expiration"], 7200, creds["_window"])
    assert creds["AccountId"] == ACCOUNT
    pairs = parse_env(vault.ok("export", "inc", env={"AWS_REGION": "", "AWS_DEFAULT_REGION": ""}).stdout)
    assert pairs["AWS_REGION"] == "ap-northeast-1"
    local = parse_env(vault.ok("export", "local", env={"AWS_REGION": "", "AWS_DEFAULT_REGION": ""}).stdout)
    assert local["AWS_REGION"] == "eu-west-2"
    assert vault.sessions() == []


def test_source_profile_loop_is_reported(vault, aws):
    user = aws.user()
    vault.write_config("[profile la]\nsource_profile = lb\n\n[profile lb]\nsource_profile = lc\n\n[profile lc]\nsource_profile = la\n\n[profile base]\n")
    vault.add("base", user)
    marker = vault.root / "ran"
    for profile, chain in (("la", "la -> lb -> lc -> la"), ("lc", "lc -> la -> lb -> lc")):
        for args in (["export", profile], ["exec", profile, "--", "touch", str(marker)]):
            result = vault.run(*args)
            assert result.returncode != 0 and result.stdout == ""
            assert f"loop detected in source_profile chain: {chain}" in result.stderr
    assert not marker.exists()
    assert vault.sessions() == []


def test_config_file_location_override(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\nregion = us-west-1\n\n[profile home-only]\n")
    custom = vault.root / "conf" / "custom-config"
    write_fixture(custom, "[profile base]\nregion = eu-west-3\n\n[profile elsewhere]\nsource_profile = base\n")
    chosen = {"AWS_CONFIG_FILE": str(custom), **NO_REGION}
    vault.add("base", user, env=chosen)
    vault.add("fresh", user, env=chosen)
    assert vault.ok("list", "--profiles", env=chosen).stdout.splitlines() == ["base", "elsewhere", "fresh"]
    assert vault.ok("list", "--profiles", env=NO_REGION).stdout.splitlines() == ["base", "home-only"]
    assert region_of(vault, "base", env=chosen) == "eu-west-3"
    assert region_of(vault, "base", env=NO_REGION) == "us-west-1"
    assert identity(vault.export("elsewhere", env=chosen)) == user.arn
    assert vault.run("export", "elsewhere", env=NO_REGION).returncode != 0
    assert vault.read_as_candidate(vault.config_path) == "[profile base]\nregion = us-west-1\n\n[profile home-only]\n"

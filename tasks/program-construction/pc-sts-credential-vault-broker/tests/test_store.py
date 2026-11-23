"""The encrypted credential store: add, remove, list and passphrase/location handling."""

import configparser

from conftest import identity, parse_json_creds
from harness import run_command


def config_sections(vault):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read_string(vault.read_as_candidate(vault.config_path))
    return {name: dict(parser[name]) for name in parser.sections()}


def plaintext_hits(vault, secret, where=None):
    result = run_command(["grep", "-rlF", "--", secret, str(where or vault.home)], capture_output=True, text=True, timeout=30)
    assert result.returncode in (0, 1), result.stderr
    return result.stdout.split()


def test_add_stores_encrypted_credentials_and_adds_profile(vault, aws):
    user = aws.user()
    vault.write_config("[profile keep]\nregion = eu-west-1\n")
    result = vault.add("fresh", user)
    assert result.stdout == 'Added credentials to profile "fresh" in vault\n'
    assert vault.credentials() == ["fresh"]
    assert config_sections(vault) == {"profile keep": {"region": "eu-west-1"}, "profile fresh": {}}
    assert vault.export("fresh", "-n")["AccessKeyId"] == user.key_id
    assert plaintext_hits(vault, user.secret) == []
    stored = run_command(["find", str(vault.home / ".awsvault" / "keys"), "-type", "f"], capture_output=True, text=True, timeout=30)
    assert stored.returncode == 0 and stored.stdout.strip(), "the default store directory holds the item"

    second = aws.user()
    vault.add("quiet", second, "--no-add-config")
    assert config_sections(vault) == {"profile keep": {"region": "eu-west-1"}, "profile fresh": {}}
    assert sorted(vault.credentials()) == ["fresh", "quiet"]
    session = vault.export("quiet")
    assert identity(session) == second.arn
    for secret in (second.secret, session["SecretAccessKey"], session["SessionToken"]):
        assert plaintext_hits(vault, secret) == []


def test_add_rejections_store_nothing(vault, aws):
    user = aws.user()
    config = "[profile base]\n\n[profile ops]\nsource_profile = base\n"
    vault.write_config(config)
    vault.add("base", user)
    attempts = [
        ({"AWS_ACCESS_KEY_ID": user.key_id, "AWS_SECRET_ACCESS_KEY": ""}, "newone", "AWS_SECRET_ACCESS_KEY"),
        ({"AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": user.secret}, "newone", "AWS_ACCESS_KEY_ID"),
        ({"AWS_ACCESS_KEY_ID": user.key_id, "AWS_SECRET_ACCESS_KEY": user.secret}, "ops", "source_profile of base"),
    ]
    for env, profile, message in attempts:
        result = vault.run("add", "--env", profile, env=env)
        assert result.returncode != 0
        assert message in result.stderr
        assert vault.credentials() == ["base"]
        assert vault.read_as_candidate(vault.config_path) == config
    assert vault.export("base", "-n")["AccessKeyId"] == user.key_id


def test_readd_replaces_key_and_drops_only_that_profiles_sessions(vault, aws):
    first, second = aws.user(), aws.user()
    vault.write_config("[profile base]\n\n[profile alias]\nsource_profile = base\n")
    vault.add("base", first)
    vault.export("base")
    alias_session = vault.export("alias")
    assert len(vault.sessions()) == 2

    result = vault.add("base", second)
    assert result.stdout == 'Added credentials to profile "base" in vault\nDeleted 1 existing sessions.\n'
    assert [kind for kind, _ in vault.sessions()] == ["sts.GetSessionToken"]
    assert vault.export("base", "-n")["AccessKeyId"] == second.key_id
    assert identity(vault.export("base")) == second.arn
    # The alias profile keeps its own cached session, issued from the first key.
    again = vault.export("alias")
    assert (again["AccessKeyId"], again["Expiration"]) == (alias_session["AccessKeyId"], alias_session["Expiration"])
    assert identity(again) == first.arn
    assert len(vault.sessions()) == 2


def test_remove_force_deletes_credentials_but_keeps_sessions(vault, aws):
    user, other = aws.user(), aws.user()
    vault.write_config("[profile base]\n\n[profile other]\n")
    vault.add("base", user)
    vault.add("other", other)
    vault.export("base")
    result = vault.ok("remove", "-f", "base")
    assert result.stdout == "Deleted credentials.\n"
    assert vault.credentials() == ["other"]
    assert [kind for kind, _ in vault.sessions()] == ["sts.GetSessionToken"]
    # Without stored credentials the profile cannot be resolved, cached session or not.
    for args in (["export", "--format=json", "base"], ["export", "-n", "base"]):
        failed = vault.run(*args)
        assert failed.returncode != 0 and failed.stdout == ""
    assert vault.run("remove", "-f", "base").returncode != 0
    assert vault.ok("rm", "-f", "other").stdout == "Deleted credentials.\n"
    assert vault.credentials() == []
    assert aws.keys(user) == [user.key_id]


def test_remove_without_force_needs_a_terminal(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    vault.add("base", user)
    result = vault.run("remove", "base", stdin="y\n")
    assert result.returncode != 0
    assert "Deleted credentials" not in result.stdout
    assert vault.credentials() == ["base"]
    assert vault.export("base", "-n")["AccessKeyId"] == user.key_id


def test_wrong_passphrase_cannot_read_credentials(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    vault.add("base", user)
    session = vault.export("base")
    marker = vault.root / "ran"
    wrong = {"AWS_VAULT_FILE_PASSPHRASE": "not-the-passphrase"}
    for args in (["export", "-n", "base"], ["export", "base"],
                 ["exec", "-n", "base", "--", "touch", str(marker)],
                 ["exec", "base", "--", "touch", str(marker)]):
        result = vault.run(*args, env=wrong)
        assert result.returncode != 0
        for secret in (user.secret, user.key_id, session["AccessKeyId"],
                       session["SecretAccessKey"], session["SessionToken"]):
            assert secret not in result.stdout + result.stderr
    assert not marker.exists()
    assert [kind for kind, _ in vault.sessions()] == ["sts.GetSessionToken"]
    assert vault.export("base", "-n")["SecretAccessKey"] == user.secret


def test_store_directory_override(vault, aws):
    user = aws.user()
    vault.write_config("[profile base]\n")
    store = vault.root / "elsewhere" / "keys"
    custom = {"AWS_VAULT_FILE_DIR": str(store)}
    vault.add("base", user, env=custom)
    session = vault.export("base", env=custom)
    assert vault.ok("list", "--credentials", env=custom).stdout == "base\n"
    assert len(vault.ok("list", "--sessions", env=custom).stdout.splitlines()) == 1
    assert vault.credentials() == [] and vault.sessions() == []
    assert vault.run("export", "-n", "base").returncode != 0
    found = run_command(["find", str(store), "-type", "f"], capture_output=True, text=True, timeout=30)
    assert found.returncode == 0 and len(found.stdout.split()) >= 2
    for secret in (user.secret, session["SecretAccessKey"], session["SessionToken"]):
        assert plaintext_hits(vault, secret, vault.root) == []


def test_profile_names_are_case_sensitive(vault, aws):
    user = aws.user()
    vault.write_config("[profile Base]\nregion = eu-north-1\n")
    vault.add("base", user, "--no-add-config")
    assert vault.export("base", "-n")["AccessKeyId"] == user.key_id
    for name in ("Base", "BASE"):
        result = vault.run("export", "-n", name)
        assert result.returncode != 0 and result.stdout == ""
    assert "profile 'BASE' not found" in vault.run("export", "BASE").stderr
    assert vault.credentials() == ["base"]


def test_backend_and_store_flags(vault, aws):
    """The global --backend/--file-dir flags select the same store as their variables."""
    user = aws.user()
    vault.write_config("[profile base]\n")
    store = vault.root / "flagged"
    flags = ["--backend=file", f"--file-dir={store}"]
    no_backend = {"AWS_VAULT_BACKEND": ""}
    vault.ok(*flags, "add", "--env", "base", env={**no_backend, "AWS_ACCESS_KEY_ID": user.key_id, "AWS_SECRET_ACCESS_KEY": user.secret})
    assert vault.ok(*flags, "list", "--credentials", env=no_backend).stdout == "base\n"
    assert vault.ok("list", "--credentials", env={"AWS_VAULT_FILE_DIR": str(store)}).stdout == "base\n"
    assert vault.credentials() == []
    creds = parse_json_creds(vault.ok(*flags, "export", "--format=json", "-n", "base", env=no_backend).stdout)
    assert (creds["AccessKeyId"], creds["SecretAccessKey"]) == (user.key_id, user.secret)

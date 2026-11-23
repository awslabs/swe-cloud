"""rotate: replacing the stored key with a new IAM access key, and failures that change nothing."""

from conftest import ACCOUNT, identity


def mask(key_id):
    return "*" * 16 + key_id[-4:]


def rotation_lines(stored_as, new, old, deleted=()):
    return [
        f"Rotating credentials stored for profile '{stored_as}' using master credentials (takes 10-20 seconds)",
        "Creating a new access key",
        f"Created new access key {mask(new)}",
        f"Waiting for new access key {mask(new)} to become usable",
        *deleted,
        f"Deleting old access key {mask(old)}",
        f"Deleted old access key {mask(old)}",
        "Finished rotating access key",
    ]


def only_new_key(aws, user, old):
    keys = aws.keys(user)
    assert len(keys) == 1 and keys != [old], keys
    return keys[0]


def test_rotate_role_profile_with_master_key(vault, aws):
    user, role = aws.user(), aws.role()
    vault.write_config(f"[profile base]\n\n[profile alias]\nsource_profile = base\n\n[profile ops]\nsource_profile = base\nrole_arn = {role.arn}\n")
    vault.add("base", user)
    vault.export("base")
    alias = vault.export("alias")
    result = vault.ok("rotate", "--no-session", "ops")
    new = only_new_key(aws, user, user.key_id)
    assert result.stdout.splitlines() == rotation_lines("base", new, user.key_id, ["Deleted 1 sessions for base"])
    stored = vault.export("base", "-n")
    assert stored["AccessKeyId"] == new and stored["SecretAccessKey"] != user.secret
    assert identity(stored) == user.arn
    assert vault.credentials() == ["base"]
    # Only sessions of profiles in the rotated chain are dropped.
    sessions = vault.sessions()
    assert [kind for kind, _ in sessions] == ["sts.GetSessionToken"]
    again = vault.export("alias")
    assert (again["AccessKeyId"], again["Expiration"]) == (alias["AccessKeyId"], alias["Expiration"])
    assert f"arn:aws:sts::{ACCOUNT}:assumed-role/{role.name}/" in identity(vault.export("ops"))
    fresh = vault.export("base")
    assert identity(fresh) == user.arn


def test_rotate_through_a_role_chain_twice(vault, aws):
    user, first, second = aws.user(), aws.role(), aws.role()
    vault.write_config(f"[profile base]\n\n[profile ops]\nsource_profile = base\nrole_arn = {first.arn}\n\n[profile deep]\nsource_profile = ops\nrole_arn = {second.arn}\n")
    vault.add("base", user)
    result = vault.ok("rotate", "-n", "deep")
    middle = only_new_key(aws, user, user.key_id)
    assert result.stdout.splitlines() == rotation_lines("base", middle, user.key_id)
    vault.export("base")
    result = vault.ok("rotate", "-n", "deep")
    last = only_new_key(aws, user, middle)
    assert result.stdout.splitlines() == rotation_lines("base", last, middle, ["Deleted 1 sessions for base"])
    assert vault.export("base", "-n")["AccessKeyId"] == last
    assert vault.sessions() == []
    assert f":assumed-role/{second.name}/" in identity(vault.export("deep"))


def test_rotate_role_profile_holding_its_own_key(vault, aws):
    user, role = aws.user(), aws.role()
    other = aws.user()
    vault.write_config(f"[profile direct]\nrole_arn = {role.arn}\n\n[profile other]\n")
    vault.add("direct", user)
    vault.add("other", other)
    vault.export("other")
    result = vault.ok("rotate", "-n", "direct")
    new = only_new_key(aws, user, user.key_id)
    assert result.stdout.splitlines() == rotation_lines("direct", new, user.key_id)
    with_role = vault.export("direct", "-n")
    assert with_role["AccessKeyId"] not in (user.key_id, new) and ":assumed-role/" in identity(with_role)
    # A role profile never prints its stored key; a second rotation shows which key was stored.
    second = vault.ok("rotate", "-n", "direct")
    newest = only_new_key(aws, user, new)
    assert second.stdout.splitlines() == rotation_lines("direct", newest, new)
    assert vault.export("other", "-n")["AccessKeyId"] == other.key_id
    assert aws.keys(other) == [other.key_id]
    assert len(vault.sessions()) == 1
    assert sorted(vault.credentials()) == ["direct", "other"]


def test_failed_rotations_leave_everything_unchanged(vault, aws):
    """In this venue these forms fail at IAM; nothing may be created, stored or dropped."""
    user, role = aws.user(), aws.role()
    vault.write_config(f"[profile base]\n\n[profile ops]\nsource_profile = base\nrole_arn = {role.arn}\n")
    vault.add("base", user)
    session = vault.export("base")
    before = vault.sessions()
    for args in (["rotate", "base"], ["rotate", "--no-session", "base"], ["rotate", "ops"]):
        result = vault.run(*args)
        assert result.returncode != 0
        assert result.stderr.strip()
        assert "Finished rotating access key" not in result.stdout
        assert aws.keys(user) == [user.key_id]
        stored = vault.export("base", "-n")
        assert (stored["AccessKeyId"], stored["SecretAccessKey"]) == (user.key_id, user.secret)
        assert len(vault.sessions()) == len(before)
        again = vault.export("base")
        assert (again["AccessKeyId"], again["Expiration"]) == (session["AccessKeyId"], session["Expiration"])

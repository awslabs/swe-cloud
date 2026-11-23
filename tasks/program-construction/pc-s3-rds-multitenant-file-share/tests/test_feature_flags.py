"""Server-side feature flags (disabled / enabled / forced) and what /config reports for them."""

import json
import urllib.request

from plikenv import boolean, check_upload_flags, error, number, solo


def flags(**values):
    """TOML lines for feature flags not already set by the base configuration."""
    return "\n".join(f'{key} = "{value}"' for key, value in values.items())


def set_base_flag(plik, key, value):
    """Change one flag the base configuration already sets (avoids a duplicate TOML key)."""
    text = plik.config.read_text().replace(f'{key} = "enabled"', f'{key} = "{value}"')
    plik.config.write_text(text)


def refused_creation(alice, plik, options, message):
    error(alice.api.post("/upload", options), 400, "unable to create upload : " + message)
    assert plik.sql("SELECT count(*) FROM uploads") == [(0,)]


def test_forced_one_shot_makes_every_upload_one_shot(plik):
    alice = solo(plik, extra=flags(FeatureOneShot="forced"))
    up = alice.create_upload(oneShot=False)
    check_upload_flags(up, one_shot=True, admin=True)
    assert plik.sql("SELECT one_shot FROM uploads") == [(True,)]


def test_disabled_one_shot_refuses_one_shot_uploads(plik):
    alice = solo(plik, extra=flags(FeatureOneShot="disabled"))
    refused_creation(alice, plik, {"oneShot": True}, "one shot uploads are disabled")


def test_disabled_removable_refuses_removable_uploads(plik):
    plik.write_config()
    set_base_flag(plik, "FeatureRemovable", "disabled")
    alice = solo(plik)
    refused_creation(alice, plik, {"removable": True}, "removable uploads are disabled")


def test_forced_removable_makes_every_upload_removable(plik):
    plik.write_config()
    set_base_flag(plik, "FeatureRemovable", "forced")
    alice = solo(plik)
    up = alice.create_upload(removable=False)
    check_upload_flags(up, removable=True, admin=True)
    assert plik.sql("SELECT removable FROM uploads") == [(True,)]


def test_forced_password_refuses_unprotected_uploads(plik):
    plik.write_config()
    set_base_flag(plik, "FeaturePassword", "forced")
    alice = solo(plik)
    refused_creation(alice, plik, {}, "server only accept uploads protected by password")


def test_disabled_password_refuses_protected_uploads(plik):
    plik.write_config()
    set_base_flag(plik, "FeaturePassword", "disabled")
    alice = solo(plik)
    refused_creation(alice, plik, {"protectedByPassword": True, "password": "pw"}, "upload password protection is disabled")


def test_forced_comments_require_a_non_blank_comment(plik):
    alice = solo(plik, extra=flags(FeatureComments="forced"))
    refused_creation(alice, plik, {"comments": "   "}, "upload comments are required")


def test_disabled_comments_are_dropped(plik):
    alice = solo(plik, extra=flags(FeatureComments="disabled"))
    up = alice.create_upload(comments="not kept")
    assert up["comments"] in ("", None)
    assert plik.sql("SELECT coalesce(comments, '') FROM uploads") == [("",)]


def test_disabled_streaming_refuses_stream_uploads(plik):
    alice = solo(plik, extra=flags(FeatureStream="disabled"))
    refused_creation(alice, plik, {"stream": True}, "streaming uploads are disabled")


def test_forced_streaming_refuses_stored_uploads(plik):
    alice = solo(plik, extra=flags(FeatureStream="forced"))
    refused_creation(alice, plik, {}, "streaming uploads are required")


def test_disabled_account_deletion_refuses_delete_me(plik):
    alice = solo(plik, extra=flags(FeatureDeleteAccount="disabled"))
    error(alice.session.delete("/me"), 400, "delete account is not enabled")
    assert plik.sql("SELECT id FROM users") == [(alice.id,)]


def test_disabled_api_tokens_refuse_token_management(plik):
    plik.write_config()
    set_base_flag(plik, "FeatureApiTokens", "disabled")
    plik.add_user("alice", "alice-secret-1", "-1")
    plik.start()
    session = plik.client()
    assert session.login("alice", "alice-secret-1").status == 200
    error(session.post("/me/token", {"comment": "nope"}), 400, "API tokens are disabled")
    error(session.get("/me/token"), 400, "API tokens are disabled")
    assert plik.sql("SELECT count(*) FROM tokens") == [(0,)]


def test_config_reports_flags_and_limits(plik):
    plik.write_config(
        extra=flags(FeatureOneShot="forced", FeatureStream="disabled", FeatureComments="forced", FeatureSetTTL="disabled",
                    DefaultTTLStr="1h", MaxTTLStr="2h", MaxFileSizeStr="1KB") + "\nMaxFilePerUpload = 7"
    )
    plik.add_user("alice", "alice-secret-1")
    plik.start()
    with urllib.request.urlopen(f"{plik.base}/config", timeout=5) as resp:
        config = json.loads(resp.read())
    assert config["feature_one_shot"] == "forced"
    assert config["feature_stream"] == "disabled"
    assert config["feature_comments"] == "forced"
    assert config["feature_set_ttl"] == "disabled"
    assert config["feature_extend_ttl"] == "disabled"
    assert config["feature_delete_account"] == "enabled"
    number(config["defaultTTL"], 3600)
    number(config["maxTTL"], 7200)
    number(config["maxFileSize"], 1000)
    number(config["maxFilePerUpload"], 7)
    boolean(config["authentication"], True)

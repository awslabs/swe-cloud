"""Secrets Manager destination.

one-to-one: one secret per key, named ``schema(key)``.
many-to-one: one secret named ``schema(bundle_name)`` holding the whole source as a JSON object.
Secrets Manager has no path, so the listing covers every secret in the account and region.

Ownership is the ``syncctl:sync-id`` tag, attached by the CreateSecret call itself.

A secret deleted in the console is only scheduled for deletion: it keeps its name and its tags, and
ListSecrets hides it unless asked. Such a secret is still the entry that holds the name, so the
listing includes it and ownership decides what happens to it.

A write whose reply was lost may already have been applied when the SDK retries it. The retry then
fails although the first attempt did what was asked, so after such a failure the secret's state, not
the reply, says whether the step is done. Deletes go by ARN: a secret re-created under the same name
(a team taking the key over) has another ARN, so a re-sent delete cannot reach it.
"""

from __future__ import annotations

import json

from syncctl import aws, schema, tags


def _list_secrets(sm) -> dict[str, dict]:
    record: dict[str, dict] = {}
    kwargs: dict = {"IncludePlannedDeletion": True}
    while True:
        page = sm.list_secrets(**kwargs)
        for entry in page.get("SecretList", []):
            if entry.get("Name"):
                record[entry["Name"]] = entry
        token = page.get("NextToken")
        if not token:
            return record
        kwargs["NextToken"] = token


def _value_of(sm, name: str) -> str | None:
    try:
        return sm.get_secret_value(SecretId=name).get("SecretString")
    except Exception as exc:  # noqa: BLE001
        if aws.error_code(exc) == "ResourceNotFoundException":
            return None
        raise


def _describe(sm, name: str) -> dict:
    return sm.describe_secret(SecretId=name)


def _force_delete(sm, arn: str) -> None:
    try:
        sm.delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)
    except Exception as exc:  # noqa: BLE001
        # Gone already: an earlier attempt of this call deleted it.
        if aws.error_code(exc) != "ResourceNotFoundException":
            raise


def _restore(sm, name: str) -> None:
    try:
        sm.restore_secret(SecretId=name)
    except Exception as exc:  # noqa: BLE001
        if aws.error_code(exc) != "InvalidRequestException" or _describe(sm, name).get("DeletedDate"):
            raise


def _owned(cfg, existing: dict[str, dict]) -> dict[str, dict[str, str]]:
    out = {}
    for name, entry in existing.items():
        tag_map = tags.as_dict(entry.get("Tags"))
        if tags.owner_of(tag_map) == cfg.sync_id:
            out[name] = tag_map
    return out


def _put(sm, cfg, existing, owned, name: str, value: str, report: dict) -> None:
    wanted_tags = tags.desired_tags(cfg.sync_id, cfg.tags)
    if name in existing and name not in owned:
        report["conflicts"].append(name)
        return
    entry = existing.get(name)
    if name not in owned:
        try:
            sm.create_secret(Name=name, SecretString=value, Tags=tags.as_list(wanted_tags))
            report["created"].append(name)
            return
        except Exception as exc:  # noqa: BLE001
            # InvalidRequestException: the name is held by a secret scheduled for deletion.
            if aws.error_code(exc) not in ("ResourceExistsException", "InvalidRequestException"):
                raise
        entry = _describe(sm, name)
        tag_map = tags.as_dict(entry.get("Tags"))
        if tags.owner_of(tag_map) != cfg.sync_id:
            report["conflicts"].append(name)
            return
        owned[name] = tag_map

    if entry and entry.get("DeletedDate"):
        # Ours, deleted in the console while the source still has it: bring it back.
        _restore(sm, name)

    if _value_of(sm, name) != value:
        sm.update_secret(SecretId=name, SecretString=value)
        report["updated"].append(name)

    to_add, to_remove = tags.diff(wanted_tags, owned[name])
    if to_add:
        sm.tag_resource(SecretId=name, Tags=tags.as_list(to_add))
    if to_remove:
        sm.untag_resource(SecretId=name, TagKeys=to_remove)


def _bundle_name(cfg) -> str:
    return schema.key_with_schema(cfg.bundle_name, cfg.environment, cfg.key_schema)


def _desired(cfg, source: dict[str, str]) -> dict[str, str]:
    if cfg.mapping == "one-to-one":
        return {k: v for k, v in schema.add_schema(source, cfg.environment, cfg.key_schema).items() if v}
    return {_bundle_name(cfg): json.dumps(source, sort_keys=True)}


def sync(cfg, source: dict[str, str], report: dict) -> None:
    sm = aws.client("secretsmanager")
    existing = _list_secrets(sm)
    owned = _owned(cfg, existing)
    desired = _desired(cfg, source)

    for name, value in desired.items():
        _put(sm, cfg, existing, owned, name, value, report)

    if cfg.disable_secret_deletion:
        return
    for name in list(owned):
        if name not in desired:
            _force_delete(sm, existing[name]["ARN"])
            report["deleted"].append(name)


def remove(cfg, source: dict[str, str], report: dict) -> None:
    sm = aws.client("secretsmanager")
    existing = _list_secrets(sm)
    for name in _owned(cfg, existing):
        _force_delete(sm, existing[name]["ARN"])
        report["deleted"].append(name)

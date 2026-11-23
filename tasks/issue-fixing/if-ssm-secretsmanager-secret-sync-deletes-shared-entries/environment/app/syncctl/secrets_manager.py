"""Secrets Manager destination.

one-to-one: one secret per key, named ``schema(key)``.
many-to-one: one secret named ``schema(bundle_name)`` holding the whole source as a JSON object.
Secrets Manager has no path, so the listing covers every secret in the account and region.
"""

from __future__ import annotations

import json

from syncctl import aws, schema, tags


def _list_secrets(sm, cfg) -> dict[str, dict]:
    record: dict[str, dict] = {}
    kwargs: dict = {}
    while True:
        page = sm.list_secrets(**kwargs)
        for entry in page.get("SecretList", []):
            name = entry.get("Name")
            if name and schema.matches_schema(name, cfg.environment, cfg.key_schema):
                record[name] = entry
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


def _force_delete(sm, name: str) -> None:
    sm.delete_secret(SecretId=name, ForceDeleteWithoutRecovery=True)


def _put(sm, cfg, existing: dict[str, dict], name: str, value: str, report: dict) -> None:
    if name in existing:
        if _value_of(sm, name) != value:
            sm.update_secret(SecretId=name, SecretString=value)
            report["updated"].append(name)
    else:
        sm.create_secret(Name=name, SecretString=value)
        report["created"].append(name)

    current = tags.as_dict(existing.get(name, {}).get("Tags"))
    to_add, to_remove = tags.diff(tags.desired_tags(cfg.sync_id, cfg.tags), current)
    if to_add:
        sm.tag_resource(SecretId=name, Tags=tags.as_list(to_add))
    if to_remove:
        sm.untag_resource(SecretId=name, TagKeys=to_remove)


def _bundle_name(cfg) -> str:
    return schema.key_with_schema(cfg.bundle_name, cfg.environment, cfg.key_schema)


def sync(cfg, source: dict[str, str], report: dict) -> None:
    sm = aws.client("secretsmanager")
    existing = _list_secrets(sm, cfg)

    if cfg.mapping == "one-to-one":
        desired = schema.add_schema(source, cfg.environment, cfg.key_schema)
        for name, value in desired.items():
            if not value:
                continue
            _put(sm, cfg, existing, name, value, report)

        if cfg.disable_secret_deletion:
            return
        for name in existing:
            if not desired.get(name):
                _force_delete(sm, name)
                report["deleted"].append(name)
        return

    # many-to-one
    bundle = _bundle_name(cfg)
    _put(sm, cfg, existing, bundle, json.dumps(source, sort_keys=True), report)

    if cfg.disable_secret_deletion:
        return
    # Secrets left over from an earlier mapping of this sync.
    for name in existing:
        if name == bundle:
            continue
        _force_delete(sm, name)
        report["deleted"].append(name)


def remove(cfg, source: dict[str, str], report: dict) -> None:
    sm = aws.client("secretsmanager")
    if cfg.mapping == "many-to-one":
        bundle = _bundle_name(cfg)
        _force_delete(sm, bundle)
        report["deleted"].append(bundle)
        return

    existing = _list_secrets(sm, cfg)
    desired = schema.add_schema(source, cfg.environment, cfg.key_schema)
    for name in existing:
        if name in desired:
            _force_delete(sm, name)
            report["deleted"].append(name)

"""SSM Parameter Store destination.

Parameters live under ``cfg.path`` (which may be ``/``), named ``path + schema(key)``. Values are
written as SecureString.

Ownership is the ``syncctl:sync-id`` tag. It is attached by the PutParameter call that creates the
name, so an entry is never visible without it, whatever point a run dies at.

A parameter is addressed only by its name. Once a delete is applied the name is free, and the team
taking the key over may create theirs at once; a delete sent again after a lost reply would remove
that one. So deletes are never re-sent blindly: after an unknown outcome the names are re-read and
only those still carrying this sync's tag are deleted again. Creates and updates can be retried by
the SDK: while our parameter holds the name nobody else can take it.
"""

from __future__ import annotations

import time

from syncctl import aws, schema, tags

BATCH_SIZE = 10
DELETE_ATTEMPTS = 6


def _list_parameters(ssm, cfg) -> dict[str, dict]:
    """Parameters one level below the sync's full path, keyed by full name."""
    full = schema.full_path(cfg.path, cfg.key_schema, cfg.environment)
    record: dict[str, dict] = {}
    kwargs = {"Path": full, "Recursive": False, "WithDecryption": True, "MaxResults": BATCH_SIZE}
    while True:
        page = ssm.get_parameters_by_path(**kwargs)
        for parameter in page.get("Parameters", []):
            if parameter.get("Name"):
                record[parameter["Name"]] = parameter
        token = page.get("NextToken")
        if not token:
            return record
        kwargs["NextToken"] = token


def _tags_of_name(ssm, name: str) -> dict[str, str] | None:
    try:
        resp = ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=name)
    except Exception as exc:  # noqa: BLE001
        if aws.error_code(exc) in ("InvalidResourceId", "ParameterNotFound"):
            return None
        raise
    return tags.as_dict(resp.get("TagList"))


def _value_of(ssm, name: str) -> str | None:
    try:
        return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"].get("Value")
    except Exception as exc:  # noqa: BLE001
        if aws.error_code(exc) == "ParameterNotFound":
            return None
        raise


def _delete(ssm, cfg, names: list[str]) -> list[str]:
    once = aws.client("ssm", sdk_retries=False)
    deleted = []
    for i in range(0, len(names), BATCH_SIZE):
        batch = names[i: i + BATCH_SIZE]
        for attempt in range(DELETE_ATTEMPTS):
            try:
                deleted.extend(once.delete_parameters(Names=batch).get("DeletedParameters", []))
                break
            except Exception as exc:  # noqa: BLE001
                if not aws.outcome_unknown(exc) or attempt == DELETE_ATTEMPTS - 1:
                    raise
            time.sleep(0.2 * 2 ** attempt)
            # Gone: our delete went through. Held by anyone but us: not ours any more, leave it.
            still = []
            for name in batch:
                tag_map = _tags_of_name(ssm, name)
                if tag_map is None:
                    deleted.append(name)
                elif tags.owner_of(tag_map) == cfg.sync_id:
                    still.append(name)
            batch = still
            if not batch:
                break
    return deleted


def _owned(ssm, cfg, existing: dict[str, dict]) -> dict[str, dict[str, str]]:
    """Tags of every listed parameter this sync owns."""
    out = {}
    for name in existing:
        tag_map = _tags_of_name(ssm, name)
        if tag_map is not None and tags.owner_of(tag_map) == cfg.sync_id:
            out[name] = tag_map
    return out


def _reconcile_tags(ssm, name: str, wanted: dict[str, str], current: dict[str, str]) -> None:
    to_add, to_remove = tags.diff(wanted, current)
    if to_add:
        ssm.add_tags_to_resource(ResourceType="Parameter", ResourceId=name, Tags=tags.as_list(to_add))
    if to_remove:
        ssm.remove_tags_from_resource(ResourceType="Parameter", ResourceId=name, TagKeys=to_remove)


def sync(cfg, source: dict[str, str], report: dict) -> None:
    ssm = aws.client("ssm")
    desired = {
        f"{cfg.path}{key}": value
        for key, value in schema.add_schema(source, cfg.environment, cfg.key_schema).items()
        if value  # empty values are not allowed by SSM
    }

    existing = _list_parameters(ssm, cfg)
    owned = _owned(ssm, cfg, existing)
    wanted_tags = tags.desired_tags(cfg.sync_id, cfg.tags)

    for name, value in desired.items():
        if name in existing and name not in owned:
            report["conflicts"].append(name)
            continue

        current_value = existing[name].get("Value") if name in owned else None
        if name not in owned:
            try:
                ssm.put_parameter(
                    Name=name, Value=value, Type="SecureString", Overwrite=False,
                    Tags=tags.as_list(wanted_tags),
                )
                report["created"].append(name)
                continue
            except Exception as exc:  # noqa: BLE001
                if aws.error_code(exc) != "ParameterAlreadyExists":
                    raise
            # Appeared after the listing: only ours if it carries our tag.
            tag_map = _tags_of_name(ssm, name) or {}
            if tags.owner_of(tag_map) != cfg.sync_id:
                report["conflicts"].append(name)
                continue
            owned[name] = tag_map
            current_value = _value_of(ssm, name)

        if current_value != value:
            ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
            report["updated"].append(name)
        _reconcile_tags(ssm, name, wanted_tags, owned[name])

    if cfg.disable_secret_deletion:
        return

    stale = [name for name in owned if name not in desired]
    report["deleted"].extend(_delete(ssm, cfg, stale))


def remove(cfg, source: dict[str, str], report: dict) -> None:
    ssm = aws.client("ssm")
    existing = _list_parameters(ssm, cfg)
    owned = _owned(ssm, cfg, existing)
    report["deleted"].extend(_delete(ssm, cfg, list(owned)))

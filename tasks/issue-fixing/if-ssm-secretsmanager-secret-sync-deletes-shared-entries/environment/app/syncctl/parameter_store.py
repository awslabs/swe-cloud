"""SSM Parameter Store destination.

Parameters live under ``cfg.path`` (which may be ``/``), named ``path + schema(key)``. Values are
written as SecureString.
"""

from __future__ import annotations

from syncctl import aws, schema, tags

BATCH_SIZE = 10


def _list_parameters(ssm, cfg) -> dict[str, dict]:
    """Parameters one level below the sync's full path, keyed by schema-applied secret key."""
    path = cfg.path
    full = schema.full_path(path, cfg.key_schema, cfg.environment)
    record: dict[str, dict] = {}
    kwargs = {"Path": full, "Recursive": False, "WithDecryption": True, "MaxResults": BATCH_SIZE}
    while True:
        page = ssm.get_parameters_by_path(**kwargs)
        for parameter in page.get("Parameters", []):
            name = parameter.get("Name")
            if name:
                # no leading slash if path is '/'
                key = name[len(path):] if len(full) > 1 else name
                record[key] = parameter
        token = page.get("NextToken")
        if not token:
            return record
        kwargs["NextToken"] = token


def _tags_of(ssm, record: dict[str, dict]) -> dict[str, dict[str, str]]:
    out = {}
    for key, parameter in record.items():
        resp = ssm.list_tags_for_resource(ResourceType="Parameter", ResourceId=parameter["Name"])
        out[key] = tags.as_dict(resp.get("TagList"))
    return out


def _delete(ssm, names: list[str]) -> list[str]:
    deleted = []
    for i in range(0, len(names), BATCH_SIZE):
        resp = ssm.delete_parameters(Names=names[i: i + BATCH_SIZE])
        deleted.extend(resp.get("DeletedParameters", []))
    return deleted


def sync(cfg, source: dict[str, str], report: dict) -> None:
    ssm = aws.client("ssm")
    desired = schema.add_schema(source, cfg.environment, cfg.key_schema)

    existing = _list_parameters(ssm, cfg)
    existing_tags = _tags_of(ssm, existing)
    wanted_tags = tags.desired_tags(cfg.sync_id, cfg.tags)

    for key, value in desired.items():
        if not value:  # empty values are not allowed by SSM
            continue
        name = f"{cfg.path}{key}"
        is_new = key not in existing
        if is_new or existing[key].get("Value") != value:
            ssm.put_parameter(Name=name, Value=value, Type="SecureString", Overwrite=True)
            report["created" if is_new else "updated"].append(name)

        to_add, to_remove = tags.diff(wanted_tags, existing_tags.get(key, {}))
        if to_add:
            ssm.add_tags_to_resource(ResourceType="Parameter", ResourceId=name, Tags=tags.as_list(to_add))
        if to_remove:
            ssm.remove_tags_from_resource(ResourceType="Parameter", ResourceId=name, TagKeys=to_remove)

    if cfg.disable_secret_deletion:
        return

    stale = [
        parameter["Name"]
        for key, parameter in existing.items()
        if schema.matches_schema(key, cfg.environment, cfg.key_schema)
        and not desired.get(key)
    ]
    report["deleted"].extend(_delete(ssm, stale))


def remove(cfg, source: dict[str, str], report: dict) -> None:
    ssm = aws.client("ssm")
    desired = schema.add_schema(source, cfg.environment, cfg.key_schema)
    existing = _list_parameters(ssm, cfg)
    names = [parameter["Name"] for key, parameter in existing.items() if key in desired]
    report["deleted"].extend(_delete(ssm, names))

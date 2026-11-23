"""Resource tags syncctl manages on the entries it writes."""

from __future__ import annotations

# Tag recording which sync an entry belongs to (see README, "Ownership").
OWNER_TAG_KEY = "syncctl:sync-id"


def desired_tags(sync_id: str, configured: dict[str, str]) -> dict[str, str]:
    return {**configured, OWNER_TAG_KEY: sync_id}


def as_dict(tag_list: list[dict] | None) -> dict[str, str]:
    return {t["Key"]: t.get("Value", "") for t in (tag_list or [])}


def as_list(tags: dict[str, str]) -> list[dict]:
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def diff(wanted: dict[str, str], current: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """Tags to add or change, and tag keys to remove, to turn ``current`` into ``wanted``."""
    to_add = {k: v for k, v in wanted.items() if current.get(k) != v}
    to_remove = [k for k in current if k not in wanted]
    return to_add, to_remove

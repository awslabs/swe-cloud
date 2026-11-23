"""Sync configuration (see README, "Configuration")."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


class ConfigError(ValueError):
    pass


@dataclass
class SyncConfig:
    sync_id: str
    destination: str
    environment: str = ""
    path: str = "/"
    key_schema: str | None = None
    mapping: str = "one-to-one"
    bundle_name: str | None = None
    tags: dict[str, str] = field(default_factory=dict)
    disable_secret_deletion: bool = False
    source_file: str = ""


def load(config_path: str) -> SyncConfig:
    with open(config_path, encoding="utf-8") as fh:
        raw = json.load(fh)
    try:
        cfg = SyncConfig(**raw)
    except TypeError as exc:
        raise ConfigError(f"invalid config {config_path}: {exc}") from exc

    if not cfg.sync_id:
        raise ConfigError("sync_id is required")
    if cfg.destination not in ("ssm", "secretsmanager"):
        raise ConfigError("destination must be 'ssm' or 'secretsmanager'")
    if cfg.destination == "ssm" and not (cfg.path.startswith("/") and cfg.path.endswith("/")):
        raise ConfigError("ssm path must start and end with '/'")
    if cfg.destination == "secretsmanager":
        if cfg.mapping not in ("one-to-one", "many-to-one"):
            raise ConfigError("mapping must be 'one-to-one' or 'many-to-one'")
        if cfg.mapping == "many-to-one" and not cfg.bundle_name:
            raise ConfigError("bundle_name is required for many-to-one")
    if not cfg.source_file:
        raise ConfigError("source_file is required")
    if not os.path.isabs(cfg.source_file):
        cfg.source_file = os.path.join(os.path.dirname(os.path.abspath(config_path)), cfg.source_file)
    return cfg


def load_source(cfg: SyncConfig) -> dict[str, str]:
    with open(cfg.source_file, encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise ConfigError("source file must be a JSON object of string values")
    return data

"""Runtime configuration, read from the environment (the deployment sets these)."""

import os
from dataclasses import dataclass


def _float(name, default):
    value = os.environ.get(name)
    return float(value) if value not in (None, "") else default


def _int(name, default):
    value = os.environ.get(name)
    return int(value) if value not in (None, "") else default


def _bool(name, default):
    value = os.environ.get(name)
    if value in (None, ""):
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    listen: str
    s3_endpoint: str
    bucket: str
    prefix: str
    disk_dir: str
    disk_max_bytes: int
    update_timestamps: bool
    cas_refresh_age: float
    num_uploaders: int
    max_queued_uploads: int
    proxy_contains_workers: int

    @property
    def host(self):
        return self.listen.rsplit(":", 1)[0]

    @property
    def port(self):
        return int(self.listen.rsplit(":", 1)[1])


def from_env():
    return Config(
        listen=os.environ.get("LISTEN", "127.0.0.1:9090"),
        s3_endpoint=os.environ.get("S3_ENDPOINT", "http://localhost:4566"),
        bucket=os.environ.get("BUCKET", "bazel-cache"),
        prefix=os.environ.get("S3_PREFIX", ""),
        disk_dir=os.environ.get("DISK_DIR", "/var/cache/cachesvc"),
        disk_max_bytes=_int("DISK_MAX_BYTES", 1 << 30),
        update_timestamps=_bool("S3_UPDATE_TIMESTAMPS", False),
        # Seconds. 0 disables. Ignored when S3_UPDATE_TIMESTAMPS is set.
        cas_refresh_age=_float("CAS_REFRESH_AGE", 0.0),
        num_uploaders=_int("NUM_UPLOADERS", 4),
        max_queued_uploads=_int("MAX_QUEUED_UPLOADS", 1000),
        proxy_contains_workers=_int("PROXY_CONTAINS_WORKERS", 16),
    )

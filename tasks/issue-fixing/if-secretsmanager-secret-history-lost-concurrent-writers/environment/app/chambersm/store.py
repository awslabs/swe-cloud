"""Secrets Manager store for chambersm.

Every key of a service lives in one secret named after the service. The SecretString is a JSON
object mapping each key to its value, plus one reserved field, ``_chamber_metadata``, holding a
stringified JSON object ``key -> {"created", "created_by", "version"}``. ``version`` is the
per-key version number shown by ``history`` and accepted by ``read --version``.

Older versions of the secret are kept reachable with custom staging labels so ``history`` can
replay them.
"""

import datetime
import json
import os

import boto3
from botocore.exceptions import ClientError

# All chambersm metadata is stored stringified in this field of the SecretString.
METADATA_KEY = "_chamber_metadata"


class SecretNotFound(Exception):
    """The service or key does not exist (or has no value)."""


class StoreError(Exception):
    """A write was refused."""


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def _error_code(err):
    return err.response.get("Error", {}).get("Code", "")


def json_to_secret_value_object(raw):
    """Parse a SecretString into {key: str}.

    Secrets created outside chambersm (for example by RDS) store numbers and booleans as JSON
    scalars, so they are converted to strings the same way chamber does.
    """
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("secret value is not a JSON object")
    obj = {}
    for key, value in parsed.items():
        if isinstance(value, bool):
            obj[key] = "true" if value else "false"
        elif isinstance(value, (int, float)):
            obj[key] = format(value, "g") if isinstance(value, float) else str(value)
        elif isinstance(value, str):
            obj[key] = value
        else:
            obj[key] = ""
    return obj


def rehydrate_metadata(raw_metadata):
    metadata = json.loads(raw_metadata)
    if metadata is None:
        return {}
    return metadata


def dehydrate_metadata(metadata):
    return json.dumps(metadata, separators=(",", ":"))


def get_hydrated_metadata(obj):
    raw = obj.get(METADATA_KEY)
    if raw is None:
        return {}
    return rehydrate_metadata(raw)


def get_hydrated_key_metadata(obj, key):
    return get_hydrated_metadata(obj).get(key, {"created": None, "created_by": "", "version": 0})


def change_type(version):
    return "Created" if version == 1 else "Updated"


class SecretsManagerStore:
    def __init__(self, client=None):
        # The SDK resolves endpoint, region and credentials from the standard AWS environment.
        self.svc = client if client is not None else boto3.client("secretsmanager")

    def current_user(self):
        return os.environ.get("CHAMBER_WRITER_ID") or "unknown"

    # ----------------------------------------------------------------------------- write

    def write(self, service, key, value):
        """Write value to service/key. If the secret already exists, write a new version.

        An empty value removes the key from the secret.
        """
        version = 1
        must_create = False
        delete_key_from_secret = len(value) == 0

        # The secret may legitimately not exist yet; that is ResourceNotFoundException.
        try:
            latest = self._read_latest(service)
        except SecretNotFound:
            if delete_key_from_secret:
                raise
            latest = {}
        except ClientError as err:
            if delete_key_from_secret:
                raise
            if _error_code(err) != "ResourceNotFoundException":
                raise
            latest = {}
            must_create = True

        if delete_key_from_secret:
            if key not in latest:
                raise SecretNotFound(f"{service}/{key}")
            del latest[key]
            metadata = get_hydrated_metadata(latest)
            metadata.pop(key, None)
            latest[METADATA_KEY] = dehydrate_metadata(metadata)
        else:
            user = self.current_user()
            metadata = get_hydrated_metadata(latest)
            if key in metadata:
                version = metadata[key]["version"] + 1
            metadata[key] = {"created": _now(), "created_by": user, "version": version}
            latest[key] = value
            latest[METADATA_KEY] = dehydrate_metadata(metadata)

        contents = json.dumps(latest)

        if must_create:
            self.svc.create_secret(Name=service, SecretString=contents)
        else:
            # We refuse to write to secrets with rotation enabled.
            details = self.svc.describe_secret(SecretId=service)
            if details.get("RotationEnabled"):
                raise StoreError("Cannot write to a secret with rotation enabled")
            self.svc.put_secret_value(
                SecretId=service,
                SecretString=contents,
                VersionStages=["AWSCURRENT", "CHAMBER" + str(version)],
            )

    def delete(self, service, key):
        return self.write(service, key, "")

    # ------------------------------------------------------------------------------ read

    def read(self, service, key, version=-1):
        """Read service/key; version -1 means the current value."""
        if version == -1:
            latest = self._read_latest(service)
            if key not in latest:
                raise SecretNotFound(f"{service}/{key}")
            meta = get_hydrated_key_metadata(latest, key)
            return {
                "key": key,
                "value": latest[key],
                "version": meta.get("version", 0),
                "created": meta.get("created"),
                "created_by": meta.get("created_by", ""),
            }
        return self._read_version(service, key, version)

    def _read_version(self, service, key, version):
        resp = self.svc.list_secret_version_ids(SecretId=service, IncludeDeprecated=False)
        for history in resp["Versions"]:
            item = self.svc.get_secret_value(SecretId=service, VersionId=history["VersionId"])
            if not item.get("SecretString"):
                continue
            history_item = json_to_secret_value_object(item["SecretString"])
            meta = get_hydrated_key_metadata(history_item, key)
            if meta.get("version") == version:
                if key not in history_item:
                    raise SecretNotFound(f"{service}/{key}@{version}")
                return {
                    "key": key,
                    "value": history_item[key],
                    "version": version,
                    "created": meta.get("created"),
                    "created_by": meta.get("created_by", ""),
                }
        raise SecretNotFound(f"{service}/{key}@{version}")

    def _read_latest(self, service):
        resp = self.svc.get_secret_value(SecretId=service)
        if not resp.get("SecretString"):
            raise SecretNotFound(service)
        return json_to_secret_value_object(resp["SecretString"])

    def list(self, service, include_values=False):
        latest = self._read_latest(service)
        metadata = get_hydrated_metadata(latest)
        out = []
        for key in sorted(latest):
            if key == METADATA_KEY:
                continue
            meta = metadata.get(key, {})
            entry = {
                "key": key,
                "version": meta.get("version", 0),
                "created": meta.get("created"),
                "created_by": meta.get("created_by", ""),
            }
            if include_values:
                entry["value"] = latest[key]
            out.append(entry)
        return out

    def list_raw(self, service):
        latest = self._read_latest(service)
        return {k: v for k, v in latest.items() if k != METADATA_KEY}

    # --------------------------------------------------------------------------- history

    def history(self, service, key):
        """Return the change events of service/key, one per version, oldest first."""
        resp = self.svc.list_secret_version_ids(SecretId=service, IncludeDeprecated=False)

        # Deduplicate: a write only bumps the version of the key it edits, so every later
        # version of the secret repeats the entries of the keys it did not touch.
        events = {}
        for history in resp["Versions"]:
            item = self.svc.get_secret_value(SecretId=service, VersionId=history["VersionId"])
            if not item.get("SecretString"):
                continue
            history_item = json_to_secret_value_object(item["SecretString"])
            metadata = get_hydrated_metadata(history_item)
            if key not in metadata:
                continue
            meta = metadata[key]
            this_version = meta["version"]
            if this_version not in events:
                events[this_version] = {
                    "event": change_type(this_version),
                    "version": this_version,
                    "value": history_item.get(key),
                    "created": meta.get("created"),
                    "created_by": meta.get("created_by", ""),
                }

        if not events:
            raise SecretNotFound(f"{service}/{key}")
        return [events[v] for v in sorted(events)]

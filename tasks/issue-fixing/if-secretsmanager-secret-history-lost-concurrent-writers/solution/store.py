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
import time
import uuid

import boto3
from botocore.exceptions import ClientError

# All chambersm metadata is stored stringified in this field of the SecretString.
METADATA_KEY = "_chamber_metadata"

# Custom labels that keep a version listed. Secrets Manager allows 20 staging labels across all
# versions of a secret, AWSCURRENT and AWSPREVIOUS included, so once a secret is full each write
# reuses the label of the oldest version still listed instead of adding one.
HISTORY_LABEL_PREFIX = "CHAMBER"
LABEL_QUOTA = 20
AWS_LABELS = ("AWSCURRENT", "AWSPREVIOUS", "AWSPENDING")

# Namespace for deriving the VersionId of the version that follows a given one.
_SUCCESSOR_NAMESPACE = uuid.UUID("5f0e1c3a-9b7d-4c1e-8a62-2f4d3b9c7e10")

# Give up well inside the CI step timeout.
WRITE_BUDGET_SECONDS = 15


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


def successor_id(version_id):
    """VersionId of the version written on top of version_id (32 hex characters)."""
    return uuid.uuid5(_SUCCESSOR_NAMESPACE, version_id).hex


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

        Every write reads AWSCURRENT (version P) and creates its successor with
        ClientRequestToken = successor_id(P). All writers derive that id the same way, so of two
        writers that read the same P only the first PutSecretValue creates a version; the other
        is rejected (ResourceExistsException) before Secrets Manager moves any label, and
        retries on top of the winner. The labels a put names are therefore only ever moved by a
        writer whose view of the secret was current: a fresh label while the secret has room,
        otherwise the label of the oldest version still listed.
        """
        delete_key_from_secret = len(value) == 0
        deadline = time.monotonic() + WRITE_BUDGET_SECONDS
        while True:
            if time.monotonic() > deadline:
                raise StoreError(f"could not write {service}/{key}: too much contention")
            try:
                current = self.svc.get_secret_value(SecretId=service)
            except ClientError as err:
                if _error_code(err) != "ResourceNotFoundException" or delete_key_from_secret:
                    raise
                if self._create(service, key, value):
                    return
                continue   # someone else created it first: write on top of theirs
            # We refuse to write to secrets with rotation enabled.
            details = self.svc.describe_secret(SecretId=service)
            if details.get("RotationEnabled"):
                raise StoreError("Cannot write to a secret with rotation enabled")
            stages = details.get("VersionIdsToStages", {})
            parent = current["VersionId"]
            if parent not in stages:
                continue   # AWSCURRENT moved between the two reads
            # The version we build on must keep a label of its own once AWSCURRENT leaves it.
            # Only a writer that died between CreateSecret and labelling leaves it without.
            if not any(s not in AWS_LABELS for s in stages[parent]):
                self._label_version(service, parent, stages)
                continue
            latest = json_to_secret_value_object(current.get("SecretString") or "{}")
            metadata = get_hydrated_metadata(latest)
            if delete_key_from_secret:
                if key not in latest:
                    raise SecretNotFound(f"{service}/{key}")
                del latest[key]
                metadata.pop(key, None)
            else:
                version = metadata[key]["version"] + 1 if key in metadata else 1
                metadata[key] = {"created": _now(), "created_by": self.current_user(),
                                 "version": version}
                latest[key] = value
            latest[METADATA_KEY] = dehydrate_metadata(metadata)
            label, _ = self._history_label(service, stages, extra={"AWSPREVIOUS"},
                                           keep={parent})
            try:
                self.svc.put_secret_value(
                    SecretId=service,
                    SecretString=json.dumps(latest),
                    ClientRequestToken=successor_id(parent),
                    VersionStages=["AWSCURRENT", label],
                )
                return
            except ClientError as err:
                if _error_code(err) != "ResourceExistsException":
                    raise
                # Another writer already created the successor of what we read. Re-read.

    def _create(self, service, key, value):
        user = self.current_user()
        metadata = {key: {"created": _now(), "created_by": user, "version": 1}}
        contents = json.dumps({key: value, METADATA_KEY: dehydrate_metadata(metadata)})
        try:
            resp = self.svc.create_secret(Name=service, SecretString=contents)
        except ClientError as err:
            if _error_code(err) == "ResourceExistsException":
                return False
            raise
        # CreateSecret cannot attach custom labels. If we die before this call, the next writer
        # attaches it (see write()).
        stages = self.svc.describe_secret(SecretId=service).get("VersionIdsToStages", {})
        self._label_version(service, resp["VersionId"], stages)
        return True

    def _label_version(self, service, version_id, stages):
        """Give version_id a history label; tolerate a concurrent writer doing the same."""
        label, holder = self._history_label(service, stages, keep={version_id})
        kwargs = {"RemoveFromVersionId": holder} if holder else {}
        try:
            # Conditional either way: a fresh label is only attached if nobody holds it, a reused
            # one only moves if it is still on the version we chose.
            self.svc.update_secret_version_stage(SecretId=service, VersionStage=label,
                                                 MoveToVersionId=version_id, **kwargs)
        except ClientError as err:
            if _error_code(err) not in ("InvalidParameterException", "LimitExceededException"):
                raise

    def _history_label(self, service, stages, extra=(), keep=()):
        """(label, holder): a label to put on a new version and the version it comes from.

        A fresh label while the secret stays within its label quota, otherwise the label of the
        oldest listed version outside keep.
        """
        in_use = {s for labels in stages.values() for s in labels}
        if len(in_use | set(extra)) + 1 <= LABEL_QUOTA:
            n = 1
            while HISTORY_LABEL_PREFIX + str(n) in in_use:
                n += 1
            return HISTORY_LABEL_PREFIX + str(n), None
        created = {v["VersionId"]: v.get("CreatedDate") for v in self._listed_versions(service)}
        candidates = [vid for vid, labels in stages.items()
                      if vid not in keep and vid in created
                      and any(s not in AWS_LABELS for s in labels)]
        oldest = min(candidates, key=lambda vid: (created[vid], vid))
        label = sorted(s for s in stages[oldest] if s not in AWS_LABELS)[0]
        return label, oldest

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

    def _listed_versions(self, service):
        """Every version Secrets Manager still lists (has at least one staging label)."""
        kwargs = {"SecretId": service, "IncludeDeprecated": False, "MaxResults": 100}
        while True:
            resp = self.svc.list_secret_version_ids(**kwargs)
            yield from resp["Versions"]
            if not resp.get("NextToken"):
                return
            kwargs["NextToken"] = resp["NextToken"]

    def _read_version(self, service, key, version):
        for history in self._listed_versions(service):
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
        # Deduplicate: a write only bumps the version of the key it edits, so every later
        # version of the secret repeats the entries of the keys it did not touch.
        events = {}
        for history in self._listed_versions(service):
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

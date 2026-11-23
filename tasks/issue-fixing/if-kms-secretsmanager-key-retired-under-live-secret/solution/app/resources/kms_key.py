"""KMSKey: customer managed keys. Removal schedules the key for deletion.

ScheduleKeyDeletion takes effect at once: from that moment the key is PendingDeletion and refuses
every cryptographic operation for the whole waiting period. CancelKeyDeletion is the only way back
and leaves the key Disabled.
"""
from libnuke.errors import HoldResource
from libnuke.registry import Registration

from .aws import client, error_code
from .consumers import DiscoveryError, live_consumers

TYPE = "KMSKey"
DEFAULT_PENDING_WINDOW_DAYS = 7


class KMSKey:
    def __init__(self, key_id, arn, state, manager, tags, settings):
        self.id = key_id
        self.arn = arn
        self.state = state
        self.manager = manager
        self.tags = tags
        self.settings = settings

    def string(self):
        return self.id

    def properties(self):
        props = {"ID": self.id, "ARN": self.arn, "State": self.state, "Manager": self.manager}
        for tag in self.tags:
            props[f"tag:{tag['TagKey']}"] = tag.get("TagValue", "")
        return props

    def filter(self):
        if self.state in ("PendingDeletion", "PendingReplicaDeletion"):
            return f"is already in {self.state} state"
        if self.manager == "AWS":
            return "cannot delete AWS managed key"
        return None

    def remove(self):
        # Decided now, from the account as it is, on every pass: held keys are retried next pass
        # and re-listed by the next sweep.
        try:
            consumers = live_consumers(self.id)
        except DiscoveryError as err:
            raise HoldResource(f"cannot tell whether the key is in use: {err}")
        if consumers:
            shown = ", ".join(sorted(consumers)[:5]) + (" ..." if len(consumers) > 5 else "")
            raise HoldResource(f"in use by {shown}")
        client("kms").schedule_key_deletion(
            KeyId=self.id,
            PendingWindowInDays=int(self.settings.get("pending_window_days", DEFAULT_PENDING_WINDOW_DAYS)),
        )


class KMSKeyLister:
    def list(self, settings):
        kms = client("kms")
        resources = []
        for page in kms.get_paginator("list_keys").paginate():
            for entry in page.get("Keys", []):
                try:
                    meta = kms.describe_key(KeyId=entry["KeyId"])["KeyMetadata"]
                except Exception as err:
                    print(f"WARN {TYPE}: unable to describe key {entry['KeyId']}: {error_code(err) or err}",
                          flush=True)
                    continue
                tags = []
                if meta.get("KeyManager") == "CUSTOMER":
                    try:
                        tags = kms.list_resource_tags(KeyId=entry["KeyId"]).get("Tags", [])
                    except Exception as err:
                        print(f"WARN {TYPE}: unable to list tags of {entry['KeyId']}: {error_code(err) or err}",
                              flush=True)
                resources.append(KMSKey(meta["KeyId"], meta["Arn"], meta["KeyState"],
                                        meta.get("KeyManager"), tags, settings))
        return resources


REGISTRATION = Registration(TYPE, KMSKeyLister())

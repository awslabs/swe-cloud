from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Snapshot:
    id: int
    base_manifest_list: str
    delta_manifest_list: Optional[str]
    commit_kind: str
    time_millis: int

    @staticmethod
    def from_json(obj):
        return Snapshot(
            id=int(obj["id"]),
            base_manifest_list=obj["baseManifestList"],
            delta_manifest_list=obj.get("deltaManifestList"),
            commit_kind=obj.get("commitKind", "APPEND"),
            time_millis=int(obj.get("timeMillis", 0)),
        )

    def to_json(self):
        return {
            "id": self.id,
            "baseManifestList": self.base_manifest_list,
            "deltaManifestList": self.delta_manifest_list,
            "commitKind": self.commit_kind,
            "timeMillis": self.time_millis,
        }

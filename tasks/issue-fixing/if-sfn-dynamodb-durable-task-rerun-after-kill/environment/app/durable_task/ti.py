import json
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskInstance:
    dag_id: str
    task_id: str
    run_id: str
    map_index: int = -1
    try_number: int = 1
    max_tries: int = 1

    @property
    def key(self) -> str:
        """Partition key of this task instance's record in the task state store."""
        return json.dumps([self.dag_id, self.task_id, self.run_id, self.map_index], separators=(",", ":"))

    def identity(self) -> dict:
        return {
            "dag_id": self.dag_id,
            "task_id": self.task_id,
            "run_id": self.run_id,
            "map_index": self.map_index,
        }

    def __str__(self) -> str:
        return (
            f"{self.dag_id}.{self.task_id} run_id={self.run_id} map_index={self.map_index} "
            f"try {self.try_number}/{self.max_tries}"
        )

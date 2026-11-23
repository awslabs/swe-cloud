"""Interruption events as the monitors hand them to the drain handler."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

SQS_MONITOR_KIND = "SQS_MONITOR"
SPOT_ITN_KIND = "SPOT_ITN"


@dataclass
class InterruptionEvent:
    event_id: str
    kind: str
    monitor: str
    start_time: datetime
    instance_id: str
    node_name: str = ""
    provider_id: str = ""
    is_managed: bool = True
    description: str = ""
    in_progress: bool = False
    node_processed: bool = False
    pre_drain_task: Optional[Callable[["InterruptionEvent"], None]] = field(default=None, repr=False)
    post_drain_task: Optional[Callable[["InterruptionEvent"], None]] = field(default=None, repr=False)

"""Target and endpoint identity.

ELBv2 describes a registered target only by Id, Port, AvailabilityZone (and QuicServerId, unused
here). Endpoints and targets are matched on "Id:Port".
"""
from dataclasses import dataclass, field

DRAINING = "draining"
HEALTHY = "healthy"
NOT_REGISTERED_REASON = "Target.NotRegistered"


def identifier(ip, port):
    return f"{ip}:{int(port)}"


def parse_identifier(value):
    ip, _, port = value.rpartition(":")
    if not ip or not port.isdigit():
        raise ValueError(f"endpoint must look like ip:port, got {value!r}")
    return ip, int(port)


@dataclass
class TargetInfo:
    target: dict                      # the TargetDescription as ELBv2 returned it
    health: dict = field(default_factory=dict)

    @property
    def id(self):
        return identifier(self.target["Id"], self.target["Port"])

    def is_draining(self):
        return self.health.get("State") == DRAINING

    def is_healthy(self):
        return self.health.get("State") == HEALTHY

    def is_not_registered(self):
        return self.health.get("Reason") == NOT_REGISTERED_REASON


@dataclass(frozen=True)
class Endpoint:
    ip: str
    port: int

    @property
    def id(self):
        return identifier(self.ip, self.port)

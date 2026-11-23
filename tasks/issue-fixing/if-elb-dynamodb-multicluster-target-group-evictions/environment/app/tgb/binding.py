"""Binding file: the endpoint agent's view of one cluster's Service, written atomically by rename.

    {"generation": 7, "deleted": false, "endpoints": ["10.20.0.4:8080", ...]}
"""
import json
from dataclasses import dataclass

from .types import Endpoint, parse_identifier


@dataclass(frozen=True)
class Binding:
    generation: int
    deleted: bool
    endpoints: tuple


def load_binding(path):
    with open(path) as fh:
        raw = json.load(fh)
    endpoints = []
    for value in raw.get("endpoints", []):
        ip, port = parse_identifier(value)
        endpoints.append(Endpoint(ip, port))
    return Binding(
        generation=int(raw.get("generation", 0)),
        deleted=bool(raw.get("deleted", False)),
        endpoints=tuple(sorted(set(endpoints), key=lambda e: e.id)),
    )

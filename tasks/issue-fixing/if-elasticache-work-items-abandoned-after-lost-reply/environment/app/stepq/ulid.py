"""Crockford-base32 ULIDs. stepq uses the 48-bit timestamp of a lease ID as the lease's expiry."""

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


def now_ms() -> int:
    return int(time.time() * 1000)


def new(timestamp_ms: int | None = None) -> str:
    """A fresh ULID whose timestamp is ``timestamp_ms`` (default: now)."""
    ts = now_ms() if timestamp_ms is None else int(timestamp_ms)
    value = (ts << 80) | int.from_bytes(os.urandom(10), "big")
    out = []
    for _ in range(26):
        out.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def time_ms(ulid: str) -> int:
    """The timestamp (ms) encoded in the first ten characters of ``ulid``."""
    ts = 0
    for c in ulid[:10].upper():
        ts = ts * 32 + _INDEX[c]
    return ts

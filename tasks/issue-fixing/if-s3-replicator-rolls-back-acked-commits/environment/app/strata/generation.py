"""Generation ids: UUID v7 built from an inverted timestamp.

The timestamp goes back in time, so the newest generation of a database lists first in the
bucket (S3 lists keys in lexicographic order), under the assumption that fetching the newest
generation is the most common operation.
"""
import os
import time
import uuid

_INVERT_SECONDS = 253370761200
_INVERT_NANOS = 999999999


def _new_v7(millis, rnd):
    b = bytearray(16)
    b[0:6] = millis.to_bytes(6, "big")
    b[6] = 0x70 | (rnd[0] & 0x0F)
    b[7] = rnd[1]
    b[8] = 0x80 | (rnd[2] & 0x3F)
    b[9:16] = rnd[3:10]
    return uuid.UUID(bytes=bytes(b))


def generation_from_timestamp(seconds, nanos):
    seconds, nanos = _INVERT_SECONDS - seconds, _INVERT_NANOS - nanos
    millis = seconds * 1000 + nanos // 1_000_000
    return str(_new_v7(millis, os.urandom(10)))


def generate_generation():
    now = time.time_ns()
    return generation_from_timestamp(now // 1_000_000_000, now % 1_000_000_000)


def generation_to_timestamp(generation):
    """Returns the creation time (unix seconds, float) encoded in a generation id."""
    millis = int.from_bytes(uuid.UUID(generation).bytes[0:6], "big")
    seconds, nanos = millis // 1000, (millis % 1000) * 1_000_000
    return (_INVERT_SECONDS - seconds) + (_INVERT_NANOS - nanos) / 1e9


def is_generation(text):
    try:
        return str(uuid.UUID(text)) == text
    except ValueError:
        return False

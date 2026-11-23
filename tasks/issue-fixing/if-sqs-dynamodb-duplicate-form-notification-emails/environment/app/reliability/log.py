"""Structured log lines, one JSON object per line on stderr (CloudWatch-style)."""
import json
import sys
import time


def emit(level, **fields):
    record = {"ts": round(time.time(), 3), "level": level, **fields}
    sys.stderr.write(json.dumps(record, default=str) + "\n")
    sys.stderr.flush()


def info(**fields):
    emit("info", **fields)


def warn(**fields):
    emit("warn", **fields)


def error(**fields):
    emit("error", **fields)

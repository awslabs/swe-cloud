"""Loads the queue function library (library.lua) into Valkey."""

from pathlib import Path

SOURCE = Path(__file__).with_name("library.lua")


def load(client) -> str:
    return client.function_load(SOURCE.read_text(), replace=True)

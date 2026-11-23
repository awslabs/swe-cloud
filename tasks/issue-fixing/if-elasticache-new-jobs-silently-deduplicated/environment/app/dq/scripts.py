"""Loads the Lua scripts under dq/lua/ and runs them with EVALSHA (redis-py Script objects)."""

from pathlib import Path

_LUA = Path(__file__).resolve().parent / "lua"


class Scripts:
    NAMES = ("addJob", "moveToActive", "extendLock", "moveToFinished", "moveToDelayed",
             "moveStalledJobsToWait")

    def __init__(self, conn):
        for name in self.NAMES:
            setattr(self, name, conn.register_script((_LUA / f"{name}.lua").read_text()))

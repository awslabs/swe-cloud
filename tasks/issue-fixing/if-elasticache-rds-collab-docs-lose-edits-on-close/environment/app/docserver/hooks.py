"""The extension hook chain.

Extensions run in priority order (highest first). A hook that raises stops the chain: later
extensions' hooks for that event do not run, and the error is re-raised to the caller.
"""

from __future__ import annotations

import logging

log = logging.getLogger("docserver.hooks")


class SkipFurtherHooks(Exception):
    """Raised by a hook to stop the chain on purpose: the event was handled elsewhere."""


class HookChain:
    def __init__(self, extensions):
        self.extensions = sorted(extensions, key=lambda ext: getattr(ext, "priority", 100), reverse=True)

    def run(self, name: str, payload) -> None:
        for extension in self.extensions:
            hook = getattr(extension, name, None)
            if hook is None:
                continue
            try:
                hook(payload)
            except SkipFurtherHooks:
                raise
            except Exception:
                log.exception("%s.%s failed", type(extension).__name__, name)
                raise

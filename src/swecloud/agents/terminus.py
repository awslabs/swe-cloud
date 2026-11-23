"""terminus-2 with two LLM outcomes that Harbor misses made visible.

Harbor's LiteLLM wrapper only checks for ``finish_reason == "length"``:

- A provider safety-filter decline (``finish_reason == "content_filter"``; Bedrock
  ``stopReason: content_filtered``) comes back as an empty reply. terminus-2 then
  tells the model its "JSON" was invalid and asks again, and a trial can loop like
  this for hours with nothing recorded. Here a decline raises ``ContentFilteredError``,
  so Harbor retries the same request (up to 3 x 3 times; declines are not
  deterministic) and, if every retry is declined, ends the trial with that exception.
- A reply cut off at ``max_tokens`` raises ``OutputLengthExceededError`` inside Harbor
  before its usage is added up, so its tokens and cost are missing from
  ``result.json``. Here they are added back.

Both are counted per trial in ``agent_result.metadata`` (``harness_*`` keys) and
logged to the trial's ``trial.log``.

terminus-2 also writes ``trajectory.json`` on the host into the trial's ``agent/`` folder,
which Harbor mounts read-write into the sandbox at ``/logs/agent``. Its plain
``open(path, "w")`` would follow a symlink the agent left at that path, so here the
trajectory dumps write a temp file and ``os.replace`` it onto the path.

The terminal, for a shell that keeps taking commands:

- Recording is off by default. With ``record_terminal_session`` on (Harbor's default),
  the tmux pane runs ``asciinema rec --stdin`` with the shell under it. asciinema 2.x
  relays input to the shell with blocking writes and reads none of the shell's output
  meanwhile, so one large command (about 16-23 KB, typically a heredoc that writes a
  file) fills both directions and the pane freezes for the rest of the trial; even
  ``C-c`` cannot get through. ``--ak record_terminal_session=true`` still turns it on.
- The pane's login shell runs in a loop. Without the recorder, a bare ``exit`` from
  the model ends the pane's only process, tmux closes the session, and terminus-2 ends
  the trial at once, unscored. The loop starts a fresh login shell instead, as the
  recorder's outer shell used to.
- After a command delivered as a tmux paste (keys too long for one ``send-keys``),
  the next keys first check the pane. If it sits at a ``>`` continuation prompt with
  no progress for a second, it gets ``C-c``; if it is still stuck, the shell is
  respawned. Commands typed in smaller pieces are left alone. This reads private
  ``TmuxSession`` attributes, so it only runs on the Harbor release it was written for.

The agent is otherwise the stock terminus-2.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import shlex
import tempfile
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import Any

import litellm
from harbor.agents.terminus_2 import terminus_2 as _terminus_2
from harbor.agents.terminus_2.terminus_2 import Terminus2 as _Terminus2
from harbor.agents.terminus_2.tmux_session import TmuxSession as _TmuxSession


class ContentFilteredError(RuntimeError):
    """The provider's safety filter declined to return a response."""


@dataclass
class _LLMEvents:
    logger: Any
    content_filter_declines: int = 0
    max_tokens_truncations: int = 0
    uncounted_input_tokens: int = 0
    uncounted_output_tokens: int = 0
    uncounted_cost_usd: float = 0.0
    details: list[dict] = field(default_factory=list)

    def record(self, reason: str, response: Any, model: str | None) -> None:
        usage = getattr(response, "usage", None)
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        completion = getattr(usage, "completion_tokens", 0) or 0
        try:
            cost = float(litellm.completion_cost(completion_response=response) or 0.0)
        except Exception:
            cost = 0.0
        if reason == "content_filter":
            self.content_filter_declines += 1
        else:
            self.max_tokens_truncations += 1
        self.uncounted_input_tokens += prompt
        self.uncounted_output_tokens += completion
        self.uncounted_cost_usd += cost
        self.details.append({"finish_reason": reason, "prompt_tokens": prompt, "completion_tokens": completion})
        self.logger.warning(
            f"harness: {model} returned finish_reason={reason} "
            f"(prompt_tokens={prompt}, completion_tokens={completion})"
        )


_events: contextvars.ContextVar[_LLMEvents | None] = contextvars.ContextVar("harness_llm_events", default=None)
_acompletion = litellm.acompletion


async def _acompletion_checked(*args: Any, **kwargs: Any) -> Any:
    response = await _acompletion(*args, **kwargs)
    choices = getattr(response, "choices", None) or []
    reason = getattr(choices[0], "finish_reason", None) if choices else None
    if reason in ("content_filter", "length"):
        events = _events.get()
        if events is not None:
            events.record(reason, response, kwargs.get("model"))
    if reason == "content_filter":
        raise ContentFilteredError(
            f"{kwargs.get('model')}: the provider's safety filter declined the response "
            "(finish_reason=content_filter)"
        )
    return response


# Harbor calls `litellm.acompletion` through the module attribute, so this covers it.
# It only takes effect in a process that imports this agent.
litellm.acompletion = _acompletion_checked


@contextlib.contextmanager
def _replace_on_close(file: Any, mode: str = "w", *args: Any, **kwargs: Any) -> Any:
    """Write to a temp file in the same folder, then os.replace it onto ``file``.

    The rename replaces whatever is at ``file`` (including a symlink) without following it.
    """
    path = Path(file)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, mode, *args, **kwargs) as f:
            yield f
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


@contextlib.contextmanager
def _trajectory_writes_replace_files() -> Any:
    """Route terminus-2's ``open`` calls through ``_replace_on_close`` for one dump."""
    _terminus_2.open = _replace_on_close
    try:
        yield
    finally:
        del _terminus_2.open


# The release whose private TmuxSession attributes the paste check reads.
_PASTE_CHECK_HARBOR = "0.24.0"
_SETTLE_SEC = 1.0
_LOGIN_SHELL = "'bash --login'"
_RESPAWNING_LOGIN_SHELL = shlex.quote(
    "bash -c "
    + shlex.quote('while :; do bash --login; echo "[the shell exited; a new login shell started]"; sleep 0.2; done')
)


def _at_continuation_prompt(screen: str) -> bool:
    lines = [line for line in screen.splitlines() if line.strip()]
    return bool(lines) and (lines[-1] == ">" or lines[-1].startswith("> "))


class _TerminalSession(_TmuxSession):
    """Harbor's TmuxSession with a login shell that outlives ``exit`` and a check after each paste."""

    check_pastes = version("harbor") == _PASTE_CHECK_HARBOR

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pasted = False

    @property
    def _tmux_start_session(self) -> str:
        command = super()._tmux_start_session
        if _LOGIN_SHELL not in command:
            self._logger.warning("harness: tmux start command has no 'bash --login'; an exit will end the session")
            return command
        return command.replace(_LOGIN_SHELL, _RESPAWNING_LOGIN_SHELL, 1)

    async def _paste_key(self, key: str, action: str) -> None:
        await super()._paste_key(key, action)
        self._pasted = True

    async def _stuck(self) -> bool:
        """True when the pane sits at a ``>`` prompt and shows no progress for a second."""
        before = await self.capture_pane()
        if not _at_continuation_prompt(before):
            return False
        await asyncio.sleep(_SETTLE_SEC)
        return await self.capture_pane() == before

    async def send_keys(self, keys: Any, *args: Any, **kwargs: Any) -> None:
        if self._pasted and self.check_pastes:
            self._pasted = False
            if await self._stuck():
                self._logger.warning("harness: pane stuck at '>' after a paste; sending C-c")
                await super().send_keys(["C-c"], min_timeout_sec=_SETTLE_SEC)
                if await self._stuck():
                    self._logger.warning("harness: pane still stuck after C-c; respawning its shell")
                    await self.environment.exec(
                        command=f"tmux respawn-pane -k -t {shlex.quote(self._session_name)}", user=self._user
                    )
                    await asyncio.sleep(_SETTLE_SEC)
        await super().send_keys(keys, *args, **kwargs)


class Terminus2(_Terminus2):
    """Stock terminus-2; LLM declines and truncations are surfaced and counted, trajectory writes do not
    follow symlinks, and the terminal runs unrecorded with a shell that survives ``exit`` and a stuck paste."""

    def __init__(self, *args: Any, record_terminal_session: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, record_terminal_session=record_terminal_session, **kwargs)
        if not _TerminalSession.check_pastes:
            self.logger.warning(
                f"harness: paste check skipped: written for harbor {_PASTE_CHECK_HARBOR}, running {version('harbor')}"
            )

    async def setup(self, environment: Any) -> None:
        # terminus-2 builds its TmuxSession by name at the start of setup, before its first await.
        _terminus_2.TmuxSession = _TerminalSession
        try:
            await super().setup(environment)
        finally:
            _terminus_2.TmuxSession = _TmuxSession

    def _dump_trajectory_with_continuation_index(self, continuation_index: int) -> None:
        with _trajectory_writes_replace_files():
            super()._dump_trajectory_with_continuation_index(continuation_index)

    def _save_subagent_trajectory(self, *args: Any, **kwargs: Any) -> Any:
        with _trajectory_writes_replace_files():
            return super()._save_subagent_trajectory(*args, **kwargs)

    async def run(self, instruction: str, environment: Any, context: Any) -> None:
        events = _LLMEvents(logger=self.logger)
        token = _events.set(events)
        try:
            await super().run(instruction, environment, context)
        finally:
            _events.reset(token)
            context.n_input_tokens = (context.n_input_tokens or 0) + events.uncounted_input_tokens
            context.n_output_tokens = (context.n_output_tokens or 0) + events.uncounted_output_tokens
            if events.uncounted_cost_usd:
                context.cost_usd = (context.cost_usd or 0.0) + events.uncounted_cost_usd
            context.metadata = {
                **(context.metadata or {}),
                "harness_content_filter_declines": events.content_filter_declines,
                "harness_max_tokens_truncations": events.max_tokens_truncations,
                "harness_uncounted_output_tokens": events.uncounted_output_tokens,
                "harness_llm_events": events.details,
            }

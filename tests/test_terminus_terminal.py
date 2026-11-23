"""The protocol harness's terminal: unrecorded, a shell that outlives `exit`, and a stuck paste recovered.

These drive terminus-2's real `setup()` and Harbor's real `TmuxSession` against a private tmux
server on this machine; only the sandbox's `exec` is replaced by a local shell.
"""

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from harbor.environments.base import ExecResult

from swecloud.agents import terminus

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="needs tmux")

# Harbor installs recording tools when they are missing; the tests never let it.
_INSTALLERS = re.compile(r"\b(apt-get|apk|yum|dnf|pip3?|curl|wget)\b")


class _LocalSandbox:
    def __init__(self, root: Path):
        self.root = root
        self.trial_paths = SimpleNamespace(agent_dir=root / "agent")
        self.default_user = None
        self.commands: list[str] = []
        # A short socket dir: tmux socket paths are limited to about 100 bytes.
        self._env = {**os.environ, "TMUX_TMPDIR": str(root)}
        self._env.pop("TMUX", None)

    async def exec(self, command, cwd=None, env=None, timeout_sec=None, user=None):
        self.commands.append(command)
        if _INSTALLERS.search(command):
            return ExecResult(stdout="", stderr="not installing in tests", return_code=1)
        proc = await asyncio.create_subprocess_exec(
            "bash", "-c", command, cwd=cwd or self.root, env={**self._env, **(env or {})},
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout_sec or 60)
        return ExecResult(stdout=out.decode(errors="replace"), stderr=err.decode(errors="replace"), return_code=proc.returncode)

    async def upload_file(self, source_path, target_path):
        shutil.copy(source_path, target_path)

    def sent(self, text: str) -> bool:
        return any(text in c for c in self.commands)


@pytest.fixture
def sandbox():
    root = Path(f"/tmp/swecloud-tmux-{os.getpid()}-{time.monotonic_ns()}")
    root.mkdir()
    box = _LocalSandbox(root)
    yield box
    asyncio.run(box.exec("tmux kill-server"))
    shutil.rmtree(root, ignore_errors=True)


def _agent(tmp_path, **kwargs):
    return terminus.Terminus2(logs_dir=tmp_path / "logs", model_name="openai/test-model", **kwargs)


async def _wait_for(path: Path, timeout: float = 20.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists() and path.stat().st_size:
            return True
        await asyncio.sleep(0.2)
    return False


def _heredoc(path: Path, n_bytes: int) -> tuple[str, str]:
    body = "".join(f"line {i:06d} {'x' * 40}\n" for i in range(n_bytes // 52 + 1))
    return f"cat > {path} <<'EOF'\n{body}EOF\n", body


def test_recording_is_off_unless_asked_for(tmp_path):
    assert _agent(tmp_path).options.record_terminal_session is False
    assert _agent(tmp_path, record_terminal_session=True).options.record_terminal_session is True


def test_a_large_heredoc_paste_is_written_and_the_next_command_runs(tmp_path, sandbox):
    async def scenario():
        agent = _agent(tmp_path)
        await agent.setup(sandbox)
        keys, body = _heredoc(sandbox.root / "big.txt", 64_000)
        await agent._session.send_keys([keys], min_timeout_sec=0.5)
        await agent._session.send_keys([f"wc -l < {sandbox.root}/big.txt > {sandbox.root}/next", "Enter"])
        assert await _wait_for(sandbox.root / "next")
        assert (sandbox.root / "big.txt").read_text() == body

    asyncio.run(scenario())
    assert sandbox.sent("tmux paste-buffer")
    assert not sandbox.sent("asciinema")


def test_exit_starts_a_new_shell_and_the_trial_goes_on(tmp_path, sandbox):
    async def scenario():
        agent = _agent(tmp_path, record_terminal_session=False)
        await agent.setup(sandbox)
        session = agent._session
        await session.send_keys(["exit 0", "Enter"], min_timeout_sec=1.0)
        assert await session.is_session_alive()
        await session.send_keys([f"echo alive > {sandbox.root}/after-exit", "Enter"])
        assert await _wait_for(sandbox.root / "after-exit")
        assert "a new login shell started" in await session.capture_pane()

    asyncio.run(scenario())


def test_blocking_commands_and_ctrl_c_still_work(tmp_path, sandbox):
    async def scenario():
        agent = _agent(tmp_path)
        await agent.setup(sandbox)
        session = agent._session
        await session.send_keys([f"echo blocked > {sandbox.root}/blocking", "Enter"], block=True, max_timeout_sec=10)
        assert (sandbox.root / "blocking").read_text() == "blocked\n"
        await session.send_keys(["sleep 600", "Enter"], min_timeout_sec=0.5)
        await session.send_keys(["C-c"], min_timeout_sec=0.5)
        await session.send_keys([f"echo interrupted > {sandbox.root}/after-ctrl-c", "Enter"])
        assert await _wait_for(sandbox.root / "after-ctrl-c")

    asyncio.run(scenario())


# Like a jammed pane: C-c is neither a signal nor echoed, so the screen stays at ">".
_IGNORES_CTRL_C = (
    "python3 -c 'import os, time; os.system(\"stty -isig -echo < /dev/tty\"); "
    "print(\">\", flush=True); time.sleep(600)'"
)


@pytest.mark.parametrize(
    ("case", "ctrl_c", "respawn"),
    [
        ("paste ends inside a heredoc", True, False),
        ("paste leaves a program at '>' that ignores C-c", True, True),
        ("paste finishes", False, False),
        ("small heredoc typed over several sends", False, False),
    ],
)
def test_a_pane_stuck_at_a_continuation_prompt_after_a_paste_is_recovered(tmp_path, sandbox, case, ctrl_c, respawn):
    if not terminus._TerminalSession.check_pastes:
        pytest.skip("the paste check runs only on the Harbor release it was written for")
    out = sandbox.root / "out.txt"

    async def scenario():
        agent = _agent(tmp_path)
        await agent.setup(sandbox)
        session = agent._session
        keys, body = _heredoc(out, 32_000)
        if case == "paste ends inside a heredoc":
            await session.send_keys([keys.removesuffix("EOF\n")], min_timeout_sec=0.5)
        elif case == "paste leaves a program at '>' that ignores C-c":
            await session.send_keys([_IGNORES_CTRL_C + " " + keys[keys.index("<<"):]], min_timeout_sec=1.0)
        elif case == "paste finishes":
            await session.send_keys([keys], min_timeout_sec=0.5)
        else:
            for line in (f"cat > {out} <<'EOF'", "typed", "EOF"):
                await session.send_keys([line, "Enter"], min_timeout_sec=0.3)
        await session.send_keys([f"echo ok > {sandbox.root}/next", "Enter"])
        assert await _wait_for(sandbox.root / "next")
        if case == "paste finishes":
            assert out.read_text() == body
        if case.startswith("small"):
            assert out.read_text() == "typed\n"

    asyncio.run(scenario())
    assert sandbox.sent("-- C-c") is ctrl_c
    assert sandbox.sent("respawn-pane") is respawn

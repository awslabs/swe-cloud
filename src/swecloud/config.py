"""Turn the protocol, a profile and a model into one `harbor run` command."""

from __future__ import annotations

import json
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def protocol() -> dict:
    return _load(ROOT / "protocol.toml")


TASKS = ROOT / "tasks"


def splits() -> dict[str, dict]:
    """The task sets, from tasks/manifest.toml: each lives in tasks/<split>/."""
    return _load(TASKS / "manifest.toml")["splits"]


def canary() -> str:
    """The string that marks the task sets so they can be excluded from training data (tasks/manifest.toml)."""
    return _load(TASKS / "manifest.toml")["canary"]


def task_dirs(root: Path) -> list[Path]:
    """Task folders directly under root: folders with a task.toml."""
    return sorted(p.parent for p in root.glob("*/task.toml"))


_LFS_POINTER = b"version https://git-lfs.github.com/spec/v1"


def lfs_pointers(root: Path) -> list[Path]:
    """Files under root that are still Git LFS pointers, i.e. `git lfs pull` has not fetched them."""
    found = []
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink() and path.stat().st_size < 1024:
            with path.open("rb") as f:
                if f.read(len(_LFS_POINTER)) == _LFS_POINTER:
                    found.append(path)
    return found


def split_name(name: str) -> str:
    """A split's full name, from the name itself or its shorthand (`if`, `pc`)."""
    for full, meta in splits().items():
        if name in (full, meta.get("alias")):
            return full
    raise SystemExit(f"unknown split {name!r}; choose from {', '.join(split_choices())}")


def split_choices() -> list[str]:
    return [n for full, meta in splits().items() for n in (full, meta.get("alias")) if n]


def split_of_task(task: str) -> str | None:
    """The split a task id (`swe-cloud/<alias>-<name>`) belongs to, by its folder prefix."""
    folder = folder_pattern(task)
    return next((full for full, meta in splits().items() if folder.startswith(meta["alias"] + "-")), None)


def split_dir(name: str) -> Path:
    return TASKS / split_name(name)


def folder_pattern(task: str) -> str:
    """Harbor matches local tasks by folder name; task ids are `swe-cloud/<folder>`."""
    return task.removeprefix("swe-cloud/")


def presets() -> dict[str, dict]:
    """Named run shapes (presets.toml). A preset never changes a protocol setting except attempts."""
    return _load(ROOT / "presets.toml")


# How the protocol's reasoning effort is passed to a model family (see docs/MODELS.md).
REASONING_STYLES = ("effort", "openai", "none")


def profile_names() -> list[str]:
    return sorted(path.stem for path in (ROOT / "profiles").glob("*.toml"))


def profile(name: str) -> dict:
    path = ROOT / "profiles" / f"{name}.toml"
    if not path.exists():
        raise SystemExit(f"unknown profile {name!r}; choose from {', '.join(profile_names())}")
    return _load(path)


def apply_profile_env(prof: dict, environ: dict[str, str]) -> list[str]:
    """Set the profile's environment in place and return what is missing."""
    llm = prof["llm"]
    key = llm["api_key_env"]
    for alias in llm.get("api_key_aliases", []):
        if not environ.get(key) and environ.get(alias):
            environ[key] = environ[alias]
    # Keep both in sync; AWS_REGION_NAME is given higher priority.
    region = environ.get("AWS_REGION_NAME") or environ.get("AWS_REGION")
    if region:
        environ["AWS_REGION"] = environ["AWS_REGION_NAME"] = region
    for name, value in {**protocol()["env"], **llm.get("env", {})}.items():
        environ.setdefault(name, value)
    missing = [key] if llm.get("api_key_required") and not environ.get(key) else []
    missing += [name for name in prof["sandbox"].get("required_env", []) if not environ.get(name)]
    return missing


def model_id(prof: dict, model: str) -> str:
    """The LiteLLM model string: the profile's route plus the model id on that route."""
    route = prof["llm"]["route"]
    return model if model.startswith(f"{route}/") else f"{route}/{model}"


def reasoning_efforts() -> list[str]:
    """The efforts a run may choose; the first default is the protocol's reasoning_effort."""
    llm = protocol()["llm"]
    return list(dict.fromkeys([llm["reasoning_effort"], *llm.get("reasoning_efforts", [])]))


def llm_settings(reasoning: str, effort: str | None = None) -> tuple[dict, str | None]:
    """The protocol's LiteLLM kwargs, and the reasoning_effort to pass (if any)."""
    if reasoning not in REASONING_STYLES:
        raise SystemExit(f"--reasoning must be one of {', '.join(REASONING_STYLES)}")
    proto = protocol()
    llm = {"timeout": proto["llm"]["timeout_sec"], "max_tokens": proto["llm"]["max_tokens"]}
    effort = effort or proto["llm"]["reasoning_effort"]
    if effort not in reasoning_efforts():
        raise SystemExit(f"--effort must be one of {', '.join(reasoning_efforts())}")
    if reasoning == "openai":
        llm["reasoning"] = {"effort": effort}
        return llm, None
    if reasoning == "effort":
        return llm, effort
    return llm, None


def agent_kwargs(reasoning: str, effort: str | None = None) -> list[str]:
    proto = protocol()
    llm, effort = llm_settings(reasoning, effort)
    kwargs = [f"record_terminal_session={str(proto['harness']['record_terminal_session']).lower()}"]
    if effort:
        kwargs.append(f"reasoning_effort={effort}")
    kwargs.append("llm_kwargs=" + json.dumps(llm, separators=(",", ":")))
    return kwargs


@dataclass
class RunSpec:
    profile: str
    model: str | None
    tasks: Path
    job_name: str
    jobs_dir: Path
    n_concurrent: int
    n_attempts: int | None = None
    agent: str | None = None
    n_tasks: int | None = None
    reasoning: str = "effort"
    effort: str | None = None
    include: tuple[str, ...] = ()
    extra: tuple[str, ...] = ()

    def argv(self) -> list[str]:
        proto, prof = protocol(), profile(self.profile)
        agent = self.agent or proto["harness"]["agent"]
        argv = [
            "harbor", "run",
            "--job-name", self.job_name,
            # Absolute: Harbor's `docker compose cp` resolves a relative path against the task folder.
            "--jobs-dir", str(self.jobs_dir.resolve()),
            "--path", str(self.tasks),
            "--n-attempts", str(self.n_attempts or proto["run"]["n_attempts"]),
            "--n-concurrent", str(self.n_concurrent),
            "--max-retries", str(proto["run"]["max_retries"]),
            "--timeout-multiplier", str(proto["run"]["timeout_multiplier"]),
            "--env", prof["sandbox"]["environment"],
            "--agent", agent,
        ]
        for name in self.include:
            argv += ["--include-task-name", name]
        if self.n_tasks:
            argv += ["--n-tasks", str(self.n_tasks)]
        if agent == proto["harness"]["agent"]:
            if not self.model:
                raise SystemExit("--model is required for the protocol harness")
            argv += ["--model", model_id(prof, self.model)]
            for kwarg in agent_kwargs(self.reasoning, self.effort):
                argv += ["--ak", kwarg]
        # Anything after `--` on the swecloud command line goes to harbor unchanged.
        argv += list(self.extra)
        # --yes approves Harbor's prompts, among them passing host environment variables into the tasks'
        # containers. Approve it only for the included task sets and the probe; another folder's tasks get the prompt.
        tasks = self.tasks.resolve()
        if tasks.is_relative_to(TASKS) or tasks.is_relative_to(ROOT / "probes"):
            argv.append("--yes")
        return argv


def load_dotenv(environ: dict[str, str] = os.environ, path: Path | None = None) -> None:
    """Read `.env` from the current folder, or else from the repository. Values already in the environment win."""
    if path is None:
        path = next((p for p in (Path(".env"), ROOT / ".env") if p.exists()), Path(".env"))
    if path.exists():
        from dotenv import dotenv_values

        for name, value in dotenv_values(path).items():
            if value is not None:
                environ.setdefault(name, value)

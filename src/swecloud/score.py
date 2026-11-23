"""Score Harbor job folders the same way for every profile.

A trial passes only when its verifier gives reward 1 and it ended without an
exception, so errored and timed-out trials count as failed attempts.
pass@1 is the per-task pass rate averaged over tasks; pass@k counts the tasks
with at least one pass. almost@1 is the same average over attempts whose reward
reaches the protocol's almost_reward (0.95), without an exception. The SWE-Cloud
Score weights issue-fixing pass@1 and program-construction pass@1 (protocol.toml
[scoring.score]); `swecloud score` prints it when given jobs of both splits.

`harness_events` counts LLM and harness problems that do not show up as
exceptions, so a reader can tell model failures from harness damage.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path

from swecloud.config import protocol, split_of_task


def read_text(path: Path, errors: str = "strict") -> str:
    """Read a file in a job folder without following a symbolic link at the file itself."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, encoding="utf-8", errors=errors) as f:
        return f.read()


def _harness_events(trial_dir: Path, result: dict) -> dict:
    """Per-trial counts of problems Harbor does not report as exceptions."""
    meta = (result.get("agent_result") or {}).get("metadata") or {}
    events = {
        "content_filter_declines": meta.get("harness_content_filter_declines"),
        "max_tokens_truncations": meta.get("harness_max_tokens_truncations"),
        "empty_replies": 0,
        "thinking_only_replies": 0,
        "agent_turns": 0,
        "turns_with_commands": 0,
        "parse_error_replies": 0,
        "replies_without_commands": 0,
    }
    log = trial_dir / "trial.log"
    if events["max_tokens_truncations"] is None:
        # Jobs run with the stock terminus-2 agent: Harbor logs each truncation.
        events["max_tokens_truncations"] = (
            read_text(log, errors="ignore").count("Output length exceeded") if log.exists() else 0
        )
    trajectory = trial_dir / "agent" / "trajectory.json"
    if trajectory.exists():
        try:
            steps = [s for s in json.loads(read_text(trajectory))["steps"] if s.get("source") == "agent"]
        except (json.JSONDecodeError, KeyError):
            steps = []
        events["agent_turns"] = len(steps)
        events["turns_with_commands"] = sum(
            any((c.get("arguments") or {}).get("keystrokes") for c in s.get("tool_calls") or []) for s in steps
        )
        # A reply with no text and no commands but billed output tokens. Without thinking text it is a
        # safety-filter decline that reached the agent (jobs run with the stock agent);
        # with thinking text the model ended its turn after thinking without answering.
        empty = [
            s for s in steps
            if not (s.get("message") or "").strip()
            and not s.get("tool_calls")
            and ((s.get("metrics") or {}).get("completion_tokens") or 0) > 0
        ]
        events["empty_replies"] = sum(not (s.get("reasoning_content") or "").strip() for s in empty)
        events["thinking_only_replies"] = len(empty) - events["empty_replies"]
        # The harness reads commands from the reply text; a reply it cannot parse comes back with this feedback.
        unparsed = [s for s in steps if "Previous response had parsing errors" in json.dumps(s.get("observation") or "")]
        events["parse_error_replies"] = len(unparsed)
        # A reply that parsed but sent no command and did not finish the task: the agent idles for a turn.
        counted = {id(s) for s in empty + unparsed}
        events["replies_without_commands"] = sum(
            bool(not s.get("tool_calls") and (s.get("message") or "").strip() and id(s) not in counted) for s in steps
        )
    return events


# What `score` reads in each trial folder. The sandbox can write to a trial's ``agent/`` and ``verifier/``
# folders, so a symbolic link on one of these paths could point a host-side read at any host file. Links
# elsewhere, such as in ``artifacts/app`` (the agent's own /app), are never read and are left alone.
SCORE_READS = ("result.json", "trial.log", "agent", "agent/trajectory.json")


def symlinks(job_dir: Path, reads: tuple[str, ...] = SCORE_READS) -> list[Path]:
    """Symbolic links on the paths a command reads: the job folder, each trial folder, and ``reads`` in each trial."""
    if job_dir.is_symlink():
        return [job_dir]
    if not job_dir.is_dir():
        return []  # nothing to read; e.g. a probe job that Harbor never created
    found = []
    for trial in sorted(job_dir.iterdir()):
        if trial.is_symlink():
            found.append(trial)
        elif trial.is_dir():
            found += [trial / rel for rel in reads if (trial / rel).is_symlink()]
    return found


def check_job_dir(job_dir: Path, reads: tuple[str, ...] = SCORE_READS, command: str = "score") -> None:
    links = symlinks(job_dir, reads)
    if links:
        listed = "\n  ".join(map(str, links[:20]))
        raise SystemExit(f"{job_dir}: refusing a job folder with a symbolic link on a path `{command}` reads:\n  {listed}")


def trials(job_dir: Path) -> list[dict]:
    # Checked before the glob, which would follow a trial folder that is itself a link.
    check_job_dir(job_dir)
    rows = []
    for path in sorted(job_dir.glob("*/result.json")):
        result = json.loads(read_text(path))
        rewards = (result.get("verifier_result") or {}).get("rewards") or {}
        exception = (result.get("exception_info") or {}).get("exception_type")
        reward = rewards.get("reward")
        rows.append(
            {
                "task": result["task_name"],
                "trial": result["trial_name"],
                "task_checksum": result.get("task_checksum"),
                "reward": reward,
                "exception": exception,
                "passed": reward is not None
                and reward >= protocol()["scoring"]["pass_reward"]
                and exception is None,
                "almost": reward is not None
                and reward >= protocol()["scoring"]["almost_reward"]
                and exception is None,
                "cost_usd": (result.get("agent_result") or {}).get("cost_usd"),
                "events": _harness_events(path.parent, result),
            }
        )
    return rows


def summarize(rows: list[dict]) -> dict:
    by_task: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_task[row["task"]].append(row)
    rates = {task: sum(r["passed"] for r in runs) / len(runs) for task, runs in by_task.items()}
    almost = {task: sum(r["almost"] for r in runs) / len(runs) for task, runs in by_task.items()}
    exceptions: dict[str, int] = defaultdict(int)
    for row in rows:
        if row["exception"]:
            exceptions[row["exception"]] += 1
    costs = [row["cost_usd"] for row in rows if row["cost_usd"] is not None]
    return {
        "tasks": len(by_task),
        "trials": len(rows),
        "passed_trials": sum(row["passed"] for row in rows),
        "pass@1": sum(rates.values()) / len(rates) if rates else 0.0,
        "pass@k": sum(rate > 0 for rate in rates.values()),
        "almost@1": sum(almost.values()) / len(almost) if almost else 0.0,
        "attempts_per_task": sorted({len(runs) for runs in by_task.values()}),
        "trials_with_exception": dict(exceptions),
        "timeouts_with_reward_1": sum(
            row["exception"] == "AgentTimeoutError" and row["reward"] == 1 for row in rows
        ),
        "cost_usd": round(sum(costs), 2) if costs else None,
        "harness_events": {
            "content_filter_declines": sum(r["events"]["content_filter_declines"] or 0 for r in rows),
            "trials_with_empty_replies": sum(r["events"]["empty_replies"] > 0 for r in rows),
            "empty_replies": sum(r["events"]["empty_replies"] for r in rows),
            "thinking_only_replies": sum(r["events"]["thinking_only_replies"] for r in rows),
            "max_tokens_truncations": sum(r["events"]["max_tokens_truncations"] or 0 for r in rows),
            "parse_error_replies": sum(r["events"]["parse_error_replies"] for r in rows),
            "replies_without_commands": sum(r["events"]["replies_without_commands"] for r in rows),
            # The agent declared the task done without ever sending a command.
            "trials_without_commands": sum(
                r["events"]["agent_turns"] > 0 and r["events"]["turns_with_commands"] == 0 for r in rows
            ),
        },
        "task_versions": task_versions(rows),
        "per_task": {task: f"{sum(r['passed'] for r in runs)}/{len(runs)}" for task, runs in sorted(by_task.items())},
    }


def task_versions(rows: list[dict]) -> dict:
    """Which task versions the trials ran: a digest of every task's checksum (Harbor records one per trial),
    and the tasks whose trials ran more than one version. Equal digests mean the same task files."""
    versions: dict[str, set] = defaultdict(set)
    for row in rows:
        versions[row["task"]].add(row.get("task_checksum") or "unknown")
    lines = "".join(f"{task} {checksum}\n" for task, checks in sorted(versions.items()) for checksum in sorted(checks))
    return {
        "digest": hashlib.sha256(lines.encode()).hexdigest()[:16],
        "tasks_with_several_versions": sorted(task for task, checks in versions.items() if len(checks) > 1),
    }


def swe_cloud_score(rows: list[dict]) -> dict | None:
    """The SWE-Cloud Score over trials of both splits (rows may come from several job folders), or None."""
    parts = protocol()["scoring"]["score"]
    by_split: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_split[split_of_task(row["task"])].append(row)
    if any(not by_split.get(split) for split in parts):
        return None
    out: dict = {"score": 0.0}
    for split, part in parts.items():
        summary = summarize(by_split[split])
        out[split] = {part["metric"]: summary[part["metric"]], "weight": part["weight"], "tasks": summary["tasks"],
                      "trials": summary["trials"]}
        out["score"] += part["weight"] * summary[part["metric"]]
    return out


# The `harbor run` options `swecloud run` builds itself (config.RunSpec.argv); anything else came after `--`.
_BUILT_OPTIONS = {"--job-name", "--jobs-dir", "--path", "--n-attempts", "--n-concurrent", "--max-retries",
                  "--timeout-multiplier", "--env", "--agent", "--include-task-name", "--n-tasks", "--model", "--ak", "--yes"}


def protocol_check(job_dir: Path) -> dict:
    """Whether a job ran under the protocol, from its manifest (jobs/<job>.manifest.json): the deviations, if any."""
    manifest_path = job_dir.parent / f"{job_dir.name}.manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return {"under_protocol": None, "deviations": ["no job manifest (not run with swecloud run)"]}
    manifest = json.loads(read_text(manifest_path))
    argv, proto = manifest.get("harbor_argv") or [], protocol()
    options: dict[str, list[str]] = defaultdict(list)
    extra = []
    i = 2  # after "harbor", "run"
    while i < len(argv):
        name = argv[i]
        takes_value = name != "--yes" and i + 1 < len(argv) and not argv[i + 1].startswith("--")
        value = argv[i + 1] if takes_value else ""
        (options[name].append(value) if name in _BUILT_OPTIONS else extra.append(name))
        i += 2 if takes_value else 1
    deviations = []
    expected = {"--n-attempts": str(proto["run"]["n_attempts"]), "--max-retries": str(proto["run"]["max_retries"]),
                "--timeout-multiplier": str(proto["run"]["timeout_multiplier"]), "--agent": proto["harness"]["agent"]}
    for name, want in expected.items():
        got = options.get(name, [None])[-1]
        if got != want:
            deviations.append(f"{name} {got} (protocol: {want})")
    if options.get("--n-tasks"):
        deviations.append(f"--n-tasks {options['--n-tasks'][-1]} (a subset of the task set)")
    if extra:
        deviations.append("arguments passed to harbor after `--`: " + " ".join(extra))
    effort = manifest.get("effort")
    if effort and effort not in [proto["llm"]["reasoning_effort"], *proto["llm"].get("reasoning_efforts", [])]:
        deviations.append(f"reasoning effort {effort} (not one the protocol lists)")
    return {"under_protocol": not deviations, "deviations": deviations, "effort": effort}

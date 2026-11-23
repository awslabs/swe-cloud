"""Run SWE-Cloud task sets with Harbor under the shared evaluation protocol.

Every run is one `harbor run` command; `swecloud run --dry-run` prints it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from swecloud import config, score

PROBES = config.ROOT / "probes"


def _prepare(profile_name: str) -> dict:
    config.load_dotenv()
    prof = config.profile(profile_name)
    missing = config.apply_profile_env(prof, os.environ)
    if missing:
        raise SystemExit(f"{profile_name}: set {', '.join(missing)} (in the environment or .env)")
    return prof


def _ping_model(prof: dict, model: str, reasoning: str, effort: str | None = None) -> str:
    """One short request with the protocol's own LLM settings, so a rejected parameter shows up here."""
    import litellm

    llm, effort = config.llm_settings(reasoning, effort)
    extra = {"reasoning_effort": effort} if effort else {}
    response = litellm.completion(
        model=config.model_id(prof, model),
        messages=[{"role": "user", "content": "Reply with the word OK"}],
        **{**llm, "timeout": min(llm["timeout"], 300)},
        **extra,
    )
    choice = response.choices[0]
    if choice.finish_reason not in ("stop", "end_turn", None):
        raise SystemExit(f"{model}: unexpected finish_reason {choice.finish_reason!r}")
    return (choice.message.content or "").strip()


def _check_sandbox(prof: dict) -> str:
    if prof["sandbox"]["environment"] != "docker":
        return f"{prof['sandbox']['environment']} (not checked)"
    if not shutil.which("docker"):
        raise SystemExit("docker: not found on PATH")
    info = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True
    )
    if info.returncode != 0:
        raise SystemExit(f"docker: cannot reach the daemon: {info.stderr.strip()}")
    return f"Docker Engine {info.stdout.strip()}"


def _run(spec: config.RunSpec, dry_run: bool) -> int:
    argv = spec.argv()
    print(shlex.join(argv))
    if dry_run:
        return 0
    spec.jobs_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "started_at": datetime.now(UTC).isoformat(),
        "profile": spec.profile,
        "model": spec.model,
        "reasoning": spec.reasoning if spec.model else None,
        "effort": (spec.effort or config.protocol()["llm"]["reasoning_effort"]) if spec.model else None,
        "tasks": str(spec.tasks),
        "harbor_argv": argv,
        "protocol": config.protocol(),
        # The profile's environment as the run saw it (for example the Bedrock region).
        "env": {name: os.environ.get(name) for name in config.profile(spec.profile)["llm"].get("env", {})},
        "versions": {name: version(name) for name in ("swe-cloud", "harbor", "litellm")},
    }
    (spec.jobs_dir / f"{spec.job_name}.manifest.json").write_text(json.dumps(manifest, indent=2))
    return subprocess.call(argv)


def _egress_verdict(job_dir: Path, returncode: int) -> tuple[int, str]:
    """0 when the probe ran and the sandbox is sealed, 1 when it ran and is not, 2 when the probe itself failed."""
    rows = score.trials(job_dir)
    if len(rows) != 1:
        return 2, f"egress: the probe failed (harbor exited {returncode} without a trial result), see {job_dir}"
    result = json.loads(score.read_text(next(job_dir.glob("*/result.json"))))
    error = result.get("exception_info")
    if error:
        message = (error.get("exception_message") or "").strip().splitlines()
        detail = error["exception_type"] + (f": {message[0]}" if message else "")
        return 2, f"egress: the probe failed ({detail}), see {job_dir}"
    if rows[0]["reward"] is None:
        return 2, f"egress: the probe failed (no verifier reward), see {job_dir}"
    if rows[0]["passed"]:
        return 0, "egress: no-network phase is sealed"
    return 1, f"egress: NOT sealed, see {job_dir}"


def cmd_check(args) -> int:
    prof = _prepare(args.profile)
    print(f"sandbox: {_check_sandbox(prof)}")
    model = args.model or prof["llm"].get("model")
    if model:
        print(f"model {config.model_id(prof, model)}: {_ping_model(prof, model, args.reasoning, args.effort)!r}")
    if args.egress:
        job = f"egress-probe-{args.profile}-{datetime.now(UTC):%Y%m%d-%H%M%S}"
        spec = config.RunSpec(
            profile=args.profile, model=None, tasks=PROBES, job_name=job,
            jobs_dir=Path(args.jobs_dir), n_concurrent=1, n_attempts=1, agent="oracle",
        )
        code, verdict = _egress_verdict(spec.jobs_dir / job, _run(spec, dry_run=False))
        print(verdict)
        return code
    return 0


def cmd_run(args) -> int:
    preset = config.presets()[args.preset] if args.preset else {}
    agent = args.agent or preset.get("agent")
    n_attempts = args.n_attempts or preset.get("n_attempts")
    n_tasks = args.n_tasks or preset.get("n_tasks")
    if bool(args.split) == bool(args.tasks):
        raise SystemExit("give exactly one of --split or --tasks")
    split = config.split_name(args.split) if args.split else None
    tasks = config.split_dir(split) if split else Path(args.tasks).resolve()
    if split and not config.task_dirs(tasks):
        raise SystemExit(f"tasks/{split}/ has no tasks")
    if config.lfs_pointers(tasks):
        raise SystemExit(f"{tasks}: task files are not fetched yet; run `git lfs pull`")
    prof = _prepare(args.profile)
    model = args.model or prof["llm"].get("model")
    who = re.sub(r"[^A-Za-z0-9._-]+", "-", args.model or agent or model or "agent")
    label = split or tasks.name
    job = args.job_name or "-".join([label, who, args.profile, f"{datetime.now(UTC):%Y%m%d-%H%M%S}"])
    extra = args.harbor_args[1:] if args.harbor_args[:1] == ["--"] else args.harbor_args
    spec = config.RunSpec(
        profile=args.profile, model=model, tasks=tasks, job_name=job,
        jobs_dir=Path(args.jobs_dir), n_concurrent=args.n_concurrent,
        n_attempts=n_attempts, agent=agent, n_tasks=n_tasks, reasoning=args.reasoning, effort=args.effort,
        include=tuple(config.folder_pattern(t) for t in args.task or ()), extra=tuple(extra),
    )
    return _run(spec, args.dry_run)


def cmd_score(args) -> int:
    rows = []
    for job_dir in map(Path, args.jobs):
        job_rows = score.trials(job_dir)
        rows += job_rows
        print(json.dumps({"job": job_dir.name, "protocol": score.protocol_check(job_dir), **score.summarize(job_rows)}, indent=2))
    combined = score.swe_cloud_score(rows)
    if combined:
        print(json.dumps({"swe_cloud_score": combined, "jobs": [Path(j).name for j in args.jobs]}, indent=2))
    return 0


def _compose_projects() -> list[str]:
    out = subprocess.run(
        ["docker", "compose", "ls", "--all", "--format", "json"], capture_output=True, text=True, check=True
    )
    return [p["Name"] for p in json.loads(out.stdout or "[]")]


def cmd_cleanup(args) -> int:
    """Remove Docker Compose projects left behind by a job's trials.

    An interrupted job (SIGINT, crash, host reboot) can leave a trial's containers
    (the task container, Harbor's egress sidecar) and their networks running. They hold
    bridge-network address pools, so enough leftovers make new trials fail with
    "all predefined address pools have been fully subnetted".
    """
    projects = _compose_projects()
    removed = 0
    for job_dir in map(Path, args.jobs):
        if not job_dir.is_dir():
            continue
        # cleanup reads only the names of the job's trial folders.
        score.check_job_dir(job_dir, reads=(), command="cleanup")
        running = subprocess.run(["pgrep", "-f", f"harbor run .*job-name {re.escape(job_dir.name)}( |$)"], capture_output=True)
        if running.returncode == 0 and not args.force:
            print(f"{job_dir.name}: still running; stop it first (or pass --force)")
            continue
        trials = [p.name.lower() for p in job_dir.iterdir() if p.is_dir() and "__" in p.name]
        for project in projects:
            if any(project.startswith(trial + "__") for trial in trials):
                print(("would remove " if args.dry_run else "removing ") + project)
                if not args.dry_run:
                    subprocess.run(
                        ["docker", "compose", "-p", project, "down", "--volumes", "--remove-orphans", "--rmi", "local"],
                        capture_output=True,
                    )
                removed += 1
        # Per-trial images that outlived their project (about 0.5 GB each).
        images = subprocess.run(
            ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True
        ).stdout.split()
        for image in images:
            if any(image.startswith(trial + "__") for trial in trials):
                print(("would remove image " if args.dry_run else "removing image ") + image)
                if not args.dry_run:
                    subprocess.run(["docker", "image", "rm", image], capture_output=True)
                removed += 1
    print(f"{removed} project(s)/image(s) {'found' if args.dry_run else 'removed'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="swecloud", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    profiles = config.profile_names()

    check = sub.add_parser("check", help="verify credentials, sandbox and (optionally) egress")
    check.add_argument("--profile", required=True, choices=profiles)
    check.add_argument("--model", help="also send one short request to this model id (default: the profile's model, if any)")
    check.add_argument(
        "--reasoning", choices=config.REASONING_STYLES, default="effort",
        help="how the protocol's reasoning effort is passed (see docs/MODELS.md)",
    )
    check.add_argument(
        "--effort", choices=config.reasoning_efforts(),
        help="reasoning effort (default: the protocol's, %(default)s); runs compare only at the same effort",
        default=config.protocol()["llm"]["reasoning_effort"],
    )
    check.add_argument("--egress", action="store_true", help="run the no-network probe task")
    check.add_argument("--jobs-dir", default="jobs")
    check.set_defaults(func=cmd_check)

    run = sub.add_parser(
        "run", help="run a task set under the protocol",
        epilog="Arguments after `--` are passed to harbor run unchanged.",
    )
    run.add_argument("--profile", required=True, choices=profiles)
    run.add_argument(
        "--model", help="model id on the profile's LLM route, e.g. a Bedrock inference-profile id (default: the profile's model, if any)",
    )
    run.add_argument(
        "--reasoning", choices=config.REASONING_STYLES, default="effort",
        help="how the protocol's reasoning effort is passed (see docs/MODELS.md)",
    )
    run.add_argument(
        "--effort", choices=config.reasoning_efforts(),
        help="reasoning effort (default: the protocol's, %(default)s); runs compare only at the same effort",
        default=config.protocol()["llm"]["reasoning_effort"],
    )
    where = run.add_argument_group("tasks (one of)")
    where.add_argument(
        "--split", choices=config.split_choices(),
        help="a task set in tasks/: issue-fixing (if) or program-construction (pc)",
    )
    where.add_argument("--tasks", help="any folder of Harbor tasks")
    run.add_argument("--task", action="append", metavar="NAME", help="run only this task, by id or folder name (glob; repeatable)")
    run.add_argument("--preset", choices=list(config.presets()), help="a named run shape from presets.toml")
    run.add_argument(
        "--n-concurrent", type=int, required=True,
        help="trials at once; size it to the host (per-trial reservations: docs/COMPUTE.md)",
    )
    run.add_argument("--n-attempts", type=int, help="override the protocol's attempts (smoke runs)")
    run.add_argument("--n-tasks", type=int, help="run only the first N tasks (smoke runs)")
    run.add_argument("--agent", help="override the harness, e.g. oracle")
    run.add_argument("--job-name")
    run.add_argument("--jobs-dir", default="jobs")
    run.add_argument("--dry-run", action="store_true", help="print the harbor command only")
    run.add_argument("harbor_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    run.set_defaults(func=cmd_run)

    scorer = sub.add_parser("score", help="score finished job folders")
    scorer.add_argument("jobs", nargs="+")
    scorer.set_defaults(func=cmd_score)

    cleanup = sub.add_parser("cleanup", help="remove Docker leftovers of interrupted job folders")
    cleanup.add_argument("jobs", nargs="+")
    cleanup.add_argument("--dry-run", action="store_true")
    cleanup.add_argument("--force", action="store_true", help="also clean a job that is still running")
    cleanup.set_defaults(func=cmd_cleanup)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

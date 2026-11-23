# Changelog

## 0.1.0 (2026-11-23)

First public release.

- **Task sets:** 60 issue-fixing tasks in `tasks/issue-fixing/` (1 h agent budget) and 40 program-construction tasks in `tasks/program-construction/` (4 h), each a Harbor task folder; the program-construction reference programs are stored with Git LFS.
- **The `swecloud` command:** `check` (sandbox, model route and egress probe), `run` (one `harbor run` under the protocol, recorded in a job manifest), `score` (pass@1, pass@k, almost@1, harness events, whether each job ran under the protocol, a digest of the task versions it ran, and the SWE-Cloud Score across an issue-fixing and a program-construction job) and `cleanup` (Docker leftovers of interrupted jobs). `run` takes a split (`--split issue-fixing|program-construction`, shorthands `if` and `pc`), a task filter (`--task`) and a preset (`--preset full|oracle|smoke`), and passes anything after `--` to `harbor run`.
- **Evaluation protocol** (`protocol.toml`): Harbor 0.24.0, 3 attempts per task, budgets from each task's `task.toml` with no multiplier, 1200 s LLM timeout, `max_tokens` 128000, reasoning effort xhigh by default (`--effort high` for models whose top tier is xhigh); a trial passes only with reward 1 and no exception; Harbor's usage telemetry is turned off (`HARBOR_TELEMETRY=0`).
- **Protocol harness** (`swecloud.agents.terminus:Terminus2`): terminus-2 with provider safety-filter declines and `max_tokens` truncations surfaced and counted, terminal recording off, a shell that survives a bare `exit`, and, on Harbor 0.24.0, recovery of a pane stuck at a `>` prompt after a large paste.
- **Sandbox and host safety:** every task runs with no network access and is graded in a separate container; the harness writes `trajectory.json` without following a symbolic link in the mounted log folder, and `score` and `cleanup` refuse a job folder with a symbolic link on any path they read.
- **Profiles:** `bedrock-docker` (Amazon Bedrock, local Docker) and `openrouter` (OpenRouter, local Docker).
- **Dependencies:** `filelock` is overridden to 4.2.1 or later (releases before 4.2.1 have known CVEs).
- **Installation:** run from a checkout with uv (`uv sync --locked`); not published on PyPI, and pip installs are not supported. Issue-fixing needs no Git LFS download.
- **Front page and skill:** the static website in `site/` (front page, leaderboard and task gallery) and the `swe-cloud-run` agent skill in `.agent/skills/`.
- **Published results:** the leaderboard's runs used the task versions from just before two final changes: network settings in every `task.toml` (the agent and verifier phases stay offline; the image build may use the network, for sandboxes such as Modal) and static diagrams for the issue-fixing tasks. What the agent sees and how it is graded are unchanged, so the results stand; `score` reports a different task-version digest for those jobs than for runs of the released tasks.
- **Known limitations:** four program-construction reference programs are dynamically linked, so an agent could in principle copy them with `LD_PRELOAD` (`docs/EVALUATION_PROTOCOL.md` §7); a provider outage longer than Harbor's single retry leaves errored attempts to rerun (§4.11).

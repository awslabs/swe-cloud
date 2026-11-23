# Task format and release checks

Every task is a standard [Harbor](https://github.com/harbor-framework/harbor) task folder (task schema 1.4). The task sets are in `tasks/<split>/`, one folder per task ([tasks/README.md](../tasks/README.md)).

## Folder layout

```text
tasks/<split>/<task-folder>/
  task.toml        name, metadata, time budgets, sandbox resources, network policy, health check
  instruction.md   what the agent is asked to do (the only task text the agent is given)
  environment/     Dockerfile, docker-compose.yaml, starting code (usually under app/), emulator start script
  tests/           hidden tests; test.sh runs them and writes the reward
  solution/        reference solution; solve.sh applies it (used by the oracle run)
  ATTRIBUTION.md   upstream repository, revision and licenses: for every program-construction task, and for an issue-fixing task that ports upstream code
```

- **During the agent phase** the agent sees only the instruction and the sandbox built from `environment/`.
- **After the agent finishes,** `/app` is copied into a fresh container, and `tests/` is copied in and run there (`[verifier] environment_mode = "separate"`). A task may give the verifier its own image (`[verifier.environment]`, `tests/Dockerfile`).
- **The oracle run** uses `solution/` in place of the agent.

## Two sandbox shapes

| category | containers | network | used by |
| --- | --- | --- | --- |
| single container | one: the task container runs the AWS-API emulator on `localhost` | `network_mode: none` from [`sandbox/docker-hardening.yaml`](../sandbox/docker-hardening.yaml), loopback only | issue-fixing tasks in category `aws-floci` |
| venue | `main` (the agent's container) and the emulator; plus `engines`, a private Docker daemon that starts the database, cache, search or streaming engines the emulated services need, when the task uses any | one Compose network, `venue`, either `internal: true` or a bridge with IP masquerading off: the services reach each other and nothing else | every program-construction task, and issue-fixing tasks in category `aws-floci-engines` |

The `engines` daemon runs privileged, because it starts containers itself. It holds every engine image it needs from build time, so no trial pulls from a registry.

## What the agent can use

- **Issue-fixing:** the code base in `/app` (Python), its tests and tools, and the AWS-API emulator.
- **Program-construction:** Python 3.12 (with boto3), C and C++ (with the AWS SDK for C++ 1.11.700), Go 1.25.8 and Rust 1.95.0, each with an offline dependency cache (a Go module proxy in `/opt/go-proxy`, vendored crates in `/opt/cargo-vendor`). The caches come from one dependency set shared by every program-construction task, so they reveal nothing about a task's reference program. The exact versions are recorded in `/etc/task-runtime.json` in the agent's container. The grader's image (`tests/Dockerfile`) has the same toolchains and caches, so a program that builds in the sandbox also builds in grading.

**Pinned images.** Every image a task builds from (`FROM`) or loads into the engines daemon is pinned by digest, and the `crane` tool that loads them is checked against a SHA-256 in the Dockerfile. The one exception is the emulator service in the `docker-compose.yaml` of tasks with a venue, `image: floci/floci:2.2.0`, which stays on its tag because some sandbox providers reject digests in compose files; the digest it resolved to for the release verification is `sha256:e97cd0c1dc2aa14e7697fb5ef5018404c4d6345169dbd276315f0bed2c7b0520`, the same one the single-container tasks pin. `tests/test_tasks.py` checks both rules.

**For now, program-construction images are built from the repository:** each `environment/Dockerfile` and `tests/Dockerfile` starts from a digest-pinned `python:3.12-bookworm` and installs the toolchains itself, so a first, uncached build takes several minutes (about 4 to 6 on the verification host). The plan is to move these steps into a published base image once one can be hosted, which will make builds faster and fully reproducible; until then, Debian packages follow the mirror at build time.

## `task.toml` fields used by the benchmark

| field | meaning |
| --- | --- |
| `[task] name` | Unique task id, `swe-cloud/if-<name>` or `swe-cloud/pc-<name>`. Scores are reported by this name. |
| `[metadata] task_type` | `issue_fixing` or `program_construction` |
| `[metadata] category` | `aws-floci`, or `aws-floci-engines` for an issue-fixing task with engines |
| `[metadata] tags`, `engines` | AWS services and topics; the engines a task starts |
| `[metadata.source]` | Where the task comes from: `kind` (`issue`, `pull-request` or `program` for a code base; `design`, `article`, `paper` or `postmortem` for a write-up), `project`, `url`, and either `revision` (the commit, the same one `ATTRIBUTION.md` names) or `date` (when the write-up was published). `tests/test_tasks.py` checks the form, and [tasks/SOURCES.md](../tasks/SOURCES.md) lists every task's source. |
| `[metadata] modal_vm_runtime` | `true` in every program-construction task: a note for Modal sandboxes, whose default Docker-in-Docker mode uses host networking without service-name lookup, so the task's venue needs Modal's VM runtime. Harbor 0.24 does not read the field from `task.toml`; on Modal, pass `--ek modal_vm_runtime=true`. Local Docker ignores it. |
| `[agent] timeout_sec` | Agent time budget: 3600 for issue-fixing, 14400 for program-construction |
| `[agent] user` | The unprivileged user the agent's commands run as, where the task sets one |
| `[verifier] timeout_sec` | Hidden-test time budget |
| `[verifier] environment_mode` | `separate`: the hidden tests run in a fresh container with a copy of `/app` |
| `[environment]` | `cpus`, `memory_mb`, `storage_mb`, `build_timeout_sec`, `network_mode`, `healthcheck` |
| `artifacts` | Paths saved from the sandbox after the run (usually `/app`) |

**Training-data canary.** `tasks/manifest.toml` (`canary`), `tasks/README.md` and `tasks/SOURCES.md` carry a canary string so the task sets can be excluded from training data. It is kept out of the task folders: adding it there would change every task's checksum.

## Release checks

A task is included in a release only if it passes all of these. `uv run --locked pytest tests/test_tasks.py` checks the ones marked ✓ on the task sets in `tasks/`.

1. **Oracle passes, repeatedly.** The reference solution scores 1 in at least 3 runs, at the concurrency used for evaluation, with no flaky or load-sensitive tests.
2. **Network policy.** ✓ `network_mode = "no-network"` in `[agent]` and `[verifier]`: everything the task needs is installed when the image is built, and neither the agent nor the tests have internet access. `[environment]` may be `public`, so a sandbox such as Modal can build the image; under Docker, the compose file (3) keeps the container offline throughout. This includes `tmux`, which the `terminus-2` harness needs.
3. **Sandbox.** ✓ A single-container task's `environment/docker-compose.yaml` is an unchanged copy of [`sandbox/docker-hardening.yaml`](../sandbox/docker-hardening.yaml) (no network interface, `no-new-privileges`, PID limit, dropped capabilities). ✓ A multi-container task's network is `internal: true`, or a bridge with `com.docker.network.bridge.enable_ip_masquerade: "false"`. ✓ Its `main` service has the same `no-new-privileges`, PID limit and dropped capabilities, and adds only `NET_ADMIN`. Only the `engines` daemon is privileged.
4. **File modes.** Scripts and executables carry their executable bit. Task sets are packaged with modes preserved (tar, or `zip -X`).
5. **Neutral naming.** Task names, folder names and task content (instructions, code, comments and documentation) describe the scenario. They do not name third-party companies or products. Open standards, protocols and the AWS services under test may be named.
6. **No secrets or internal data.** The task uses dummy emulator credentials only (`test` / `test`). It holds no real account ids, internal hostnames or personal data. Key material a task needs is generated for that task alone.
7. **Third-party content.** Any third-party code included in a task keeps its license file next to it, and the task's `ATTRIBUTION.md` credits it. ✓ Every program-construction task has an `ATTRIBUTION.md`, and every `ATTRIBUTION.md` names the upstream URL (`Upstream:`), the exact commit of each ported project (`Revision:`) and its licenses.
8. **Self-contained grading.** Tests are deterministic, need no network access, and run within the verifier timeout on the reference host.
9. **Separate grading.** ✓ `[verifier] environment_mode = "separate"`, and everything the tests need from the agent's work is under a declared artifact (`/app`). The oracle passes in that mode.
10. ✓ **The verifier owns the reward.** Before the tests run, the verifier (`tests/test.sh`, or `tests/verify.py` in program-construction tasks) removes `reward.txt` and `reward.json`, the files Harbor reads. Once grading runs, it always writes `reward.txt`. If the task's environment cannot start, issue-fixing tasks record 0, and program-construction tasks publish no reward, so Harbor reports an error instead of a failed attempt.
11. **Nothing done, nothing earned.** A run with Harbor's `nop` agent (`swecloud run --agent nop --n-attempts 1 …`) scores exactly 0 on the task: the tests fail on the starting code, and give no partial credit to an empty `/app`.
12. ✓ **The reference program stays private.** A program-construction task installs its reference program root-owned and execute-only, and the program is statically linked, so the agent can run it but not read or copy it. The four dynamically linked references listed in [EVALUATION_PROTOCOL.md](EVALUATION_PROTOCOL.md) §7 are the known exceptions.

## Running one task

```bash
uv run --locked swecloud run --profile bedrock-docker --preset oracle \
    --split issue-fixing --task swe-cloud/if-dynamodb-abandoned-transaction-recovery --n-concurrent 1
```

`--task` takes a glob and can be repeated, for example `--task 'swe-cloud/if-dynamodb-*'`.

---
name: swe-cloud-run
description: Set up and run the SWE-Cloud benchmark with the swecloud CLI and Harbor, step by step. Use when a user asks to install, check, run or score SWE-Cloud, or to run it on another LLM provider or sandbox.
---

# Run SWE-Cloud

SWE-Cloud is 100 standard Harbor tasks (60 issue-fixing, 40 program-construction) plus a thin CLI, `swecloud`, that pins the evaluation protocol in `protocol.toml`. This skill walks the user through one run. Work from the repository root and run every command through `uv run --locked`.

## What is tested and what is not

| | Tested by the SWE-Cloud team | Not tested |
| --- | --- | --- |
| LLM route | `bedrock-docker` and `openrouter` profiles | any other LiteLLM provider |
| Sandbox | local Docker on an x86-64 Linux host | Modal, Daytona, every other Harbor environment, Arm hosts |
| Harness and settings | `protocol.toml` as shipped, Harbor 0.24.0 (pinned in `uv.lock`) | changed harness, attempts, timeouts or LLM settings |

Tell the user this once, before step 1. Only the tested column carries a promise: scores from it are comparable with the published ones. Anything else is the user's to adapt, and you may help, but say clearly that the result is not validated and not comparable. Never present an untested combination as supported.

## Step 1. Ask the user, one question at a time

1. **API route:** Amazon Bedrock or OpenRouter. (Another provider or a non-Docker sandbox: finish this flow for the tested part if they want, then read [references/beyond-the-profiles.md](references/beyond-the-profiles.md).)
2. **Model:**
   - Bedrock: an inference-profile id, starting with `global.` or `us.`. A bare foundation-model id is rejected.
   - OpenRouter: an OpenRouter model id (required; the profile has no default model).
   - Reasoning style, only if the model needs it: `effort` (default; Claude and OpenAI models on Bedrock), `openai` (providers that take a `reasoning` object), or `none` (no effort setting; not comparable with the protocol's `xhigh`). See `docs/MODELS.md`.
3. **Split:** issue-fixing (`if`, 1 h per task), program-construction (`pc`, 4 h per task), or both (two jobs, one per split).
4. **Run shape:** `oracle` (reference solutions, validates the host), `smoke` (2 tasks, 1 attempt, checks the route) or `full` (every task, 3 attempts; the only reportable shape). Recommend oracle, then smoke, then full.
5. **Concurrency:** size `--n-concurrent` from `docs/COMPUTE.md` (about 2 vCPU and 3 to 4 GB RAM per trial, twice that for tasks with engines; above about 25 trials add Docker network pools). Ask about the host if you cannot inspect it.

Record the answers as `PROFILE` (`bedrock-docker` or `openrouter`), `MODEL`, `REASONING`, `SPLIT`, `N`.

## Step 2. Set up (same for every route)

Requirements: x86-64 Linux, Docker Engine (rootful, privileged containers allowed; PC tasks need it), [uv](https://docs.astral.sh/uv/) 0.11+, Git LFS. Recommend a dedicated VM and a low-privilege user whose only credential is the model provider's (`docs/EVALUATION_PROTOCOL.md` §7).

```bash
git clone https://github.com/awslabs/swe-cloud swe-cloud && cd swe-cloud
git lfs install && git lfs pull      # the PC reference programs
uv sync --locked
cp .env.example .env
```

Then fill `.env`, and never print a key back:
- Bedrock: `AWS_BEARER_TOKEN_BEDROCK`, or leave it empty to use the AWS credential chain. The identity needs `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream`. The region defaults to `us-east-1`; see `docs/MODELS.md` for how an environment region overrides it.
- OpenRouter: `OPENROUTER_API_KEY`.

## Step 3. Check

```bash
uv run --locked swecloud check --profile $PROFILE --model $MODEL --reasoning $REASONING --egress
```

It confirms the Docker daemon, sends one request with the protocol's own LLM settings, and runs a probe task that passes only if the sandbox has no network. Stop on any failure and fix it first: a rejected parameter here means every trial would fail.

## Step 4. Run

Validate the host once per split, then run:

```bash
uv run --locked swecloud run --profile $PROFILE --split $SPLIT --preset oracle --n-concurrent $N
uv run --locked swecloud run --profile $PROFILE --split $SPLIT --model $MODEL --reasoning $REASONING \
    --preset smoke --n-concurrent $N
uv run --locked swecloud run --profile $PROFILE --split $SPLIT --model $MODEL --reasoning $REASONING \
    --n-concurrent $N --job-name <name>
```

- Every oracle task should score 1. One that does not is broken on this host, not hard: report it, do not start a full run on top of it.
- Add `--dry-run` to print the exact `harbor run` command without running it. Show it to the user before a full run.
- `--task <glob>` (repeatable) limits a run to some tasks. Arguments after `--` go to `harbor run` unchanged.
- A full run takes hours. Start it in the background (for example `nohup ... &` or `tmux`) and tell the user where the job folder is.

**Equivalent `harbor run`.** `swecloud run` only builds one Harbor command and records it in `jobs/<job>.manifest.json`. If the user prefers Harbor directly, see [references/harbor-run.md](references/harbor-run.md).

## Step 5. Score and clean up

```bash
uv run --locked swecloud score jobs/<name>
uv run --locked swecloud cleanup jobs/<name>     # only after an interrupted job
```

`score` reports pass@1 and pass@k under the protocol's pass rule (reward 1 and no exception; timeouts and errors count as failures), and `harness_events`. A nonzero `harness_events` count means part of the result reflects the provider or harness. For PC, also report the mean reward over trials, since partial credit shows how close attempts came. File layout of a job: `docs/EVALUATION_PROTOCOL.md` §6.

## Rules

- Do not edit `protocol.toml`, `presets.toml`, the tasks or `sandbox/` for a run the user wants to compare with published scores. If they ask for a change, make it, and label the result as a modified setup.
- Do not add flags after `--` that change attempts, timeouts, the agent or the environment for a reportable run.
- Read `docs/EVALUATION_PROTOCOL.md` §4 (known issues) before debugging a failing run.

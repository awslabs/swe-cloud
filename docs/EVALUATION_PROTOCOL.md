# Evaluation protocol

Every score produced with this package uses the settings below. Two runs are comparable when only the profile (LLM route and sandbox) differs between them, at the same reasoning effort.

## 1. Settings

| setting | value | where it is set |
| --- | --- | --- |
| Harbor | 0.24.0, with LiteLLM 1.106, pinned in `uv.lock` | `pyproject.toml` |
| agent harness | `terminus-2`, through `swecloud.agents.terminus:Terminus2` (§4.1–§4.4) | `protocol.toml` |
| terminal recording | off (`record_terminal_session = false`; §4.1) | `protocol.toml` |
| LLM request timeout | 1200 s | `protocol.toml` |
| `max_tokens` | 128000 | `protocol.toml` |
| reasoning effort | xhigh by default; `--effort high` for a model whose top tier is xhigh. Passed to each model family in the way `--reasoning` selects. The job manifest records the effort, and the leaderboard shows each model's | `protocol.toml`, `--effort`, `--reasoning` |
| attempts per task | 3 | `protocol.toml` |
| retries | 1, for a trial that ends on an exception, except those Harbor never retries: agent and verifier timeouts, missing or unreadable reward files, verifier-output errors, safety refusals, authentication and model-not-found errors. A content-filter decline (`ContentFilteredError`, §4.3) is retried. Harbor waits 1 to 60 s and deletes the failed attempt's folder before the retry, so only the retry's cost and counters remain (§4.11) | `protocol.toml` |
| agent, verifier and build time budgets | each task's `task.toml`, with no multiplier | task |
| sandbox resources | each task's `[environment]` (CPU, memory, storage) | task |
| network | each task's `network_mode`, plus `network_mode: none` on a single-container task, or an internal `venue` network for a task with engines (§7). The LLM is called from the host, so the sandbox never needs a route to it. | task |
| sandbox hardening | the agent's container: `no-new-privileges`, PID limit 4096, dropped capabilities (§7) | each task's `environment/docker-compose.yaml` |
| grading | a separate verifier container that receives a copy of `/app` (§7) | each task's `[verifier] environment_mode` |
| HTTP keepalive | `AIOHTTP_SO_KEEPALIVE=true`, idle and interval 30 s, 5 probes, so long LLM requests are not dropped as idle | `protocol.toml` |
| Harbor telemetry | off (`HARBOR_TELEMETRY=0`). Harbor 0.24 otherwise sends usage data after every run: model names, token counts, cost, mean reward and task counts. A variable already set in your environment wins; a direct `harbor run` needs it set too | `protocol.toml` |

How reasoning effort reaches each model family (`--reasoning`; see [MODELS.md](MODELS.md)):

| `--reasoning` | what is sent | used for |
| --- | --- | --- |
| `effort` (default) | `--agent-kwarg reasoning_effort=xhigh` | Anthropic models, which LiteLLM sends as adaptive thinking plus `output_config.effort`. Also Bedrock `openai.*` models, which LiteLLM sends to the Bedrock OpenAI-compatible chat-completions route. |
| `openai` | `llm_kwargs.reasoning.effort = xhigh`, sent as Converse `additionalModelRequestFields` | models whose provider takes a `reasoning` object in the request body |
| `none` | nothing; the model's own default | models without an effort setting |

`check --model <model-id> --reasoning <style>` sends the same LLM settings as a run. A parameter the provider rejects therefore fails the check instead of every trial. The model id and the reasoning style are recorded in the job manifest.

## 2. Profiles

A profile sets the LLM route and the sandbox; it does not change any protocol setting.

**`bedrock-docker`** (included).
- **LLM route:** LiteLLM's `bedrock/` provider, in us-east-1. Pass an inference-profile id as `--model` (`global.` or `us.`); a bare foundation-model id is rejected for on-demand use.
- **Authentication:** a Bedrock API key in `AWS_BEARER_TOKEN_BEDROCK` (`AMAZON_BEDROCK_API_KEY` is accepted as an alias), or the standard AWS credential chain.
- **Permissions:** the identity needs `bedrock:InvokeModel` and `bedrock:InvokeModelWithResponseStream` on both the inference-profile and the foundation-model ARNs.
- **Sandboxes:** local Docker.

**`openrouter`** (included).
- **LLM route:** LiteLLM's `openrouter/` provider. Pass an OpenRouter model id as `--model` (required; the profile has no default model).
- **Authentication:** an OpenRouter API key in `OPENROUTER_API_KEY` (required).
- **Sandboxes:** local Docker.

**Other providers.** Any LiteLLM provider can be used by adding a profile file ([MODELS.md](MODELS.md#profiles)).

## 3. Scoring

- **Pass rule.** A trial passes when the verifier gives reward 1 **and** the trial ended without an exception.
- **What the reward is.**
  - **Issue-fixing:** 1 if every hidden test passes, otherwise 0.
  - **Program-construction:** partial credit, the fraction of hidden test cases that passed (skipped cases are not counted). It is exactly 1 only when every case passed, and 0 when the agent's `build.sh` fails. A program that passes most checks therefore scores just under 1 without passing. When grading cannot start because the task's services never came up, no reward is published and Harbor reports the trial as an error.

  pass@1 and pass@k count only passes. For program-construction, the mean reward over trials (each trial's `verifier_result.rewards.reward` in `result.json`) shows how close the attempts come, and is often the more informative number.
- **Timed-out and errored trials count as failed attempts.** A trial that hit its agent time limit fails even if the verifier then scores the workspace 1. `score` reports how many timeouts scored 1, so the effect of this rule is visible.
- **pass@1** = per task, passes ÷ attempts, averaged over tasks.
- **pass@k** = the number of tasks with at least one passing attempt.
- **almost@1** = per task, the share of attempts that end without an exception and reach reward 0.95 or more (at least 95% of the hidden tests, for program-construction), averaged over tasks. For issue-fixing, whose reward is 0 or 1, it equals pass@1.
- **SWE-Cloud Score** = 0.6 × issue-fixing pass@1 + 0.4 × program-construction pass@1, the headline number on the leaderboard. With 60 and 40 tasks, it equals pass@1 over all 100 tasks. almost@1 is reported alongside it, but is not part of the score. The threshold and the weights are in `protocol.toml` (`[scoring]`). Given job folders of both splits (`swecloud score <if-job> <pc-job> …`, several folders per split allowed), `score` prints it after the per-job results.
- **`task_versions`**, per job: a digest of the task versions the trials ran (from each trial's `task_checksum`), so two results compare only when their digests match. It also lists any task whose trials ran more than one version.
- **`protocol`**, per job: whether it ran under the protocol, from its manifest (`jobs/<job>.manifest.json`). It lists any deviation: other attempts, retries or timeout multiplier, another agent, a task subset (`--n-tasks`), arguments passed to Harbor after `--`, or an effort the protocol does not list. A job without a manifest is reported as unknown. A job that reruns only some attempts (for example after a provider outage) shows fewer attempts; score it together with the job it completes.
- **`harness_events`** counts problems that do not appear as exceptions:
  - provider safety-filter declines;
  - empty replies;
  - thinking-only replies;
  - `max_tokens` truncations;
  - trials whose agent never sent a command;
  - replies the harness could not parse (`parse_error_replies`): terminus-2 reads commands from a JSON object in the reply text, and a reply without one runs nothing;
  - replies that parsed but sent no command and did not finish the task (`replies_without_commands`): the agent spends a turn without acting.

  A nonzero count means part of the result reflects the provider or harness, not only the model.

## 4. Known issues and how the protocol handles them

1. **Terminal freeze.**
   - **The problem:** with terminal recording on, the tmux pane runs `asciinema rec --stdin` with the shell under it. asciinema 2.x relays input with blocking writes and reads none of the shell's output meanwhile, so any single command of about 16–23 KB (typically a heredoc that writes a file) freezes the `terminus-2` shell at a `>` prompt for the rest of the trial; even `C-c` cannot get through. Harbor 0.24 records by default.
   - **How the protocol handles it:** recording is off. `swecloud run` passes `record_terminal_session=false`, and the protocol harness also defaults to off, so a hand-written `harbor run --agent swecloud.agents.terminus:Terminus2` behaves the same. As a backstop on Harbor 0.24.0, the first keys after a command delivered as a tmux paste check the pane: one still at `>` with no progress for a second gets `C-c`, and if it is still stuck its shell is respawned. Commands typed in smaller pieces are left alone.
   - **Where to see it:** the backstop logs `harness: pane stuck at '>' after a paste` to the trial's `trial.log`. To check a job's recording setting:
     ```bash
     grep -o '"record_terminal_session": [a-z]*' jobs/<job>/config.json
     ```
2. **`exit` in the agent's shell.**
   - **The problem:** without the recorder, the login shell is the pane's only process. A bare `exit` from the model (for example at the end of a polling one-liner) ends it, tmux closes the session, and `terminus-2` ends the trial at once with reward 0.
   - **How the protocol handles it:** the pane runs its login shell in a loop, so `exit` prints `[the shell exited; a new login shell started]` and gives a fresh login shell in the starting directory, as the recorder's outer shell did. Shell state (working directory, variables, jobs) does not carry over.
3. **Provider safety-filter declines.**
   - **The problem:** a provider can decline a reply (Bedrock `stopReason: content_filtered`, LiteLLM `finish_reason: "content_filter"`) and return no text. With the stock agent the model then sees an empty reply, is told its output was invalid, and can loop for a long time.
   - **How the protocol handles it:** the decline raises `ContentFilteredError`. Harbor retries the same request up to 3 × 3 times, since declines are not deterministic. If every retry is declined, the trial ends with that exception.
   - **Where to see it:** declines are counted in `agent_result.metadata.harness_content_filter_declines` and in `score` → `harness_events`.
4. **Replies cut off at `max_tokens`.**
   - **The problem:** a model can write a single reply longer than 128k tokens. Harbor then tells the model that none of its actions ran, and asks again.
   - **How the protocol handles it:** the truncated call's tokens and cost, which Harbor does not count, are added back into `result.json`. Each truncation is counted in `harness_max_tokens_truncations`.
5. **Long replies.** A reply that takes longer than the 1200 s request timeout is cut off and retried. Raising the timeout changes the protocol.
6. **Throttling.** LiteLLM retries most transient `RateLimitError` / `ServiceUnavailableError` errors. Check your provider quotas before raising `--n-concurrent`.
7. **Docker network address pools.**
   - **The limit:** each trial creates its own Compose network, plus a second one while a separate verifier runs. Docker's default pools allow about 30 bridge networks per host; beyond that, new trials fail at startup with `all predefined address pools have been fully subnetted`.
   - **The fix:** keep concurrent trials per host below about 25, or add pools in `/etc/docker/daemon.json` and restart Docker, for example:
     ```json
     { "default-address-pools": [{ "base": "10.200.0.0/14", "size": 24 }] }
     ```
8. **Interrupted jobs.**
   - **The problem:** after SIGINT, Harbor removes each trial's `main` container but can leave the trial's other containers (such as the egress sidecar), its Compose network and its built image behind.
   - **The fix:** run `cleanup jobs/<job>` after stopping a job. It removes only Compose projects and images named after that job's trials, and refuses while the job is still running.
9. **Network access during the agent phase.** Every task's `[agent]` and `[verifier]` use `network_mode = "no-network"`. Its `[environment]` baseline is `public`, so a sandbox such as Modal can fetch packages while it builds the image; under Docker, the task's compose file keeps the container offline throughout (below). On its own, Harbor 0.24 enforces `no-network` with a small egress sidecar (with `NET_ADMIN`) whose network namespace the task container shares. That sidecar blocks TCP but still forwards DNS to the host's resolver and allows all ICMP, so `no-network` alone does not fully seal a Docker sandbox. Tasks therefore seal themselves in their own `environment/docker-compose.yaml` (§7): a single-container task gives its container `network_mode: none`, and a task with a venue keeps its services on a network with no route out. The LLM is called from the host, outside the sandbox. The egress probe (§5) checks the single-container case on your host.
10. **Timing-sensitive tests.** A few tests inject a fault into a running process at a fixed time, so their outcome can depend on host speed and load. Run the reference solutions on your host first (§5), and leave out any task they fail there.
11. **Provider outages.**
   - **The problem:** a provider outage lasts longer than the single retry, which comes 1 to 60 s later. Trials that hit it end with an exception such as `ServiceUnavailableError` and no reward, and count as failed attempts. On 2026-11-19, a 30-minute Bedrock outage affected 16 trials of the release evaluation.
   - **What to do:** after the outage, find the attempts that ended on a provider error (in each trial's `result.json`, or in `trial.log` for errors the agent met mid-run), set those trial folders aside, and rerun exactly those attempts (`--task <name> --n-attempts <k>`) into a new job. Score it together with the original job. `score` lists such a partial job's attempt count as a deviation, so the combination stays visible.

## 5. Checks before a run

```bash
uv run --locked swecloud check --profile <profile> --model <model-id> --reasoning <style>
uv run --locked swecloud check --profile <profile> --egress
uv run --locked swecloud run --profile <profile> --agent oracle \
    --tasks <tasks> --n-attempts 1 --n-concurrent 4
```

- **Egress probe:** runs a `no-network` task, with the profile's sandbox settings, whose test passes only if nothing outside the sandbox answers: HTTPS to PyPI, GitHub and 1.1.1.1 (an HTTP error status counts as an answer), name resolution through the configured resolver, DNS over UDP/53 to 1.1.1.1 and 8.8.8.8, ICMP echo, and the instance-metadata endpoint `169.254.169.254`.
- **Oracle run:** should score 1 on every task. A task that does not is broken on that host, not hard.

## 6. Reading a job

| file | contents |
| --- | --- |
| `jobs/<job>.manifest.json` | profile, model, reasoning style, the exact `harbor run` command, protocol and package versions |
| `jobs/<job>/config.json` | Harbor's resolved job config |
| `jobs/<job>/<trial>/result.json` | reward, `exception_info`, tokens, `agent_result.cost_usd`, `harness_*` counters |
| `jobs/<job>/<trial>/agent/trajectory.json` | every agent turn |
| `jobs/<job>/<trial>/verifier/` | test output |
| `jobs/<job>/<trial>/trial.log` | Harbor's log, including `harness:` lines for each decline or truncation |

## 7. Sandbox isolation

The agent runs model-generated commands, so the sandbox is treated as untrusted. This section lists what isolates it and what does not.

**Task container settings.** The sandbox is set in each task's `environment/docker-compose.yaml`, so it applies to both the agent's containers and the verifier's, with any `harbor run` command line. Run-level overlays (`--extra-docker-compose`) are not used: Harbor 0.24 does not apply them to a separate verifier.

- **Single-container tasks** carry an unchanged copy of [`sandbox/docker-hardening.yaml`](../sandbox/docker-hardening.yaml), which gives the container:
  - `network_mode: none`: loopback only (§4.9);
  - `no-new-privileges`;
  - a PID limit of 4096;
  - no `MKNOD`, `NET_RAW`, `AUDIT_WRITE`, `SYS_CHROOT` or `SETFCAP` capability.
- **Tasks with a venue** (every program-construction task, and issue-fixing tasks with engines) run their services on one network with no route out (`internal: true`, or a bridge with IP masquerading off): `main` (the agent), the emulator and, when the task needs engines, `engines`, a privileged Docker daemon that starts the engine containers. The agent can reach the engines the emulator starts, never the daemon itself. `main` has the same hardening as a single-container task, except its network, and adds `NET_ADMIN` so the venue can route it to the engine bridge. Neither kind of network gives its containers a route to the host's network or the internet. Because `engines` is privileged, tasks with engines need a Docker daemon that allows privileged containers; rootless Docker does not run them.

**Grading in a separate container.** Every task sets `[verifier] environment_mode = "separate"`. After the agent phase, Harbor copies `/app` (the task's declared artifact) out of the agent's container, and starts a fresh container from the same image. It empties `/logs/verifier` there, copies `/app` in, and runs the hidden tests. Nothing the agent left running, and nothing it wrote outside `/app`, reaches the tests.

**Folders shared with the host.** Harbor bind-mounts each trial's `agent/` and `verifier/` folders read-write into the sandbox (`/logs/agent`, `/logs/verifier`). The package handles the risks this creates:

- **Host writes.** terminus-2 writes `trajectory.json` into `agent/` from the host. The protocol harness overrides the trajectory dumps to write a temp file and `os.replace` it onto the path, so a symbolic link the agent leaves there is replaced, not followed (`src/swecloud/agents/terminus.py`).
- **Host reads.** `score` and `cleanup` refuse a job folder with a symbolic link on any path they read: the job and trial folders, and for `score` each trial's `result.json`, `trial.log`, `agent/` and `agent/trajectory.json`, which it also opens without following links. Links elsewhere, such as in `artifacts/app` (the agent's own `/app`), are never read and do not stop scoring.
- **Reward files.** Harbor reads `reward.json` before `reward.txt`. Every task's verifier removes `reward.txt` and `reward.json` before the tests run (`tests/test.sh` in issue-fixing tasks, `tests/verify.py` in program-construction tasks), and the separate verifier container also starts with an empty `/logs/verifier`. Reward integrity relies on both layers: run the tasks in separate mode, as every task's `task.toml` sets. In another mode Harbor does not empty `/logs/verifier`, and the task's own removal is the only guard.

**The agent's user.**
- **Program-construction tasks** run the agent's commands as the unprivileged user `candidate` (`[agent] user`), which owns only `/app` (and the task tool's own state directory, where a task has one). The reference program the agent must reproduce is installed root-owned and execute-only (`chmod 111 /reference/program`): `candidate` can run it to observe its behavior, but cannot read or copy the binary, as root could. Reference programs are also statically linked: a dynamically linked program would load whatever library the agent names in `LD_PRELOAD`, and that code could copy the program out of memory. **Known limitation:** four reference programs are still dynamically linked, because a static build would change their behavior or is not practical: those of `pc-cloudwatch-otlp-metrics-forwarder`, `pc-msk-rds-batch-sql-stream-processor`, `pc-rds-background-job-broker` and `pc-rds-parallel-database-cloner`. An agent could in principle copy these four this way; in the trajectories we checked (the GPT-6 Astra, GPT-6.1 Sol, GPT-6.1 Terra and Grok 4.7 runs of the release evaluation), no agent tried. When reviewing results, check trajectories for `LD_PRELOAD`. A later release will run these references outside the agent's container or replace the tasks. The emulator is started by the health check as root, so the agent cannot stop it.
- **Issue-fixing tasks** keep Harbor's default user, root, as a developer working on their own checkout would. Their sandbox holds nothing the agent should not see: the code to fix is in `/app`, the hidden tests are copied only into the verifier's container, and the reference fix (`solution/`) is used only by the oracle run.
- **In both splits** the hidden tests run as root, in their own container (above), and only that container writes the reward.

**Host.** Run the benchmark on a dedicated VM, as a dedicated low-privilege user with no credentials other than the model provider's (see [How a run works](../README.md#how-to-run-this-benchmark)), and keep the kernel and Docker up to date. Membership of the `docker` group is equivalent to root on the host. Rootless Docker suits runs of single-container tasks only; tasks with a venue need a rootful daemon that allows privileged containers, so for them the dedicated VM is the isolation boundary.

**What the tests rely on.** The hidden tests run the agent's code in `/app`, so that code runs during grading by design. The tests judge it by its behavior against the emulator, never by trusting what it reports.

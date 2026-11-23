# Beyond the tested profiles

Everything here is outside what the SWE-Cloud team validated. Tell the user that before you start, help them adapt it, and label the results as an unvalidated setup.

## How a profile works

A profile is a TOML file in `profiles/`; `--profile <name>` picks `profiles/<name>.toml`. It sets only the LLM route and the sandbox, never a protocol setting.

```toml
[llm]
route = "<litellm-prefix>"          # e.g. "bedrock", "openrouter"
api_key_env = "<KEY_VARIABLE>"
api_key_required = true
# model = "<default model id>"      # optional
# [llm.env]                         # optional, set before the run

[sandbox]
environment = "docker"              # a Harbor environment name or a module.path:ClassName import path
# required_env = ["<VARIABLE>"]     # checked before check and run
```

## Another LLM provider

Add a profile with the provider's LiteLLM prefix and key variable (`docs/MODELS.md`), keep `environment = "docker"`, and run step 3 of the skill (`check --model ... --reasoning ...`) until it passes. Only the route changes; the sandbox stays the tested one.

## Another sandbox provider

The tasks are standard Harbor tasks, so any Harbor environment can in principle run them (`uv run --locked harbor run --help` lists them under `--env`). Whether a given provider runs these tasks correctly is not tested. Things that commonly need work, from our own trials:

- **Multi-container tasks.** Every PC task and the IF tasks with engines are Docker Compose projects with an internal network and a privileged `engines` service. A provider must support Compose and privileged containers.
- **Network isolation.** Single-container tasks set `network_mode: none` in their compose file. Providers that ignore the compose file do not apply it. Always run `swecloud check --egress` with the new profile; it runs the probe on that provider.
- **Separate verifier.** Every task grades in a fresh container (`[verifier] environment_mode = "separate"`). Keep it; reward integrity depends on it (`docs/EVALUATION_PROTOCOL.md` §7).
- **Resources.** Tasks reserve up to 4 vCPU, 8 GB RAM and 24 GB disk (`task.toml` `[environment]`).
- **Image pulls.** Builds pull from Docker Hub, Debian, PyPI, GitHub, Go and Rust mirrors (`docs/COMPUTE.md`); remote builders can hit pull rate limits.

Known gaps: Modal and Daytona need extra changes to these tasks or to Harbor (for example, Harbor 0.24 does not run Compose tasks on Modal). Point the user to the provider's page in the [Harbor documentation](https://github.com/harbor-framework/harbor) and let them decide what to change. Do not claim either works.

`swecloud check` checks only the Docker daemon itself; for another environment it prints `(not checked)` and moves on to the model and egress checks.

## Worked example: Sail

We ran the oracle on both splits in Sail sandboxes, which ship their own Harbor environment. It is an example of the mechanism, not a supported profile. A profile for it looks like this:

```toml
# profiles/openrouter-sail.toml
[llm]
route = "openrouter"
api_key_env = "OPENROUTER_API_KEY"
api_key_required = true

[sandbox]
environment = "sail.harbor:SailboxEnvironment"
required_env = ["SAIL_API_KEY"]
```

Then the usual flow: `uv pip install` Sail's Python package into the project environment, set `SAIL_API_KEY` in `.env`, run `swecloud check --profile openrouter-sail --egress`, then the oracle. One constraint we hit: Sail rejects Compose image references pinned by digest, which is why the task compose files use tags. Expect constraints like this with any provider.

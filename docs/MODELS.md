# Models and profiles

The benchmark does not ship a list of models. You choose the model when you run it:

```bash
uv run --locked swecloud run --profile <profile> \
    --model <model-id> --reasoning <style> --split issue-fixing --n-concurrent 4
```

## `--model`

`--model` is the model's id on the profile's LLM route; the harness adds the route prefix itself. For example, with the `bedrock-docker` profile it is an Amazon Bedrock **inference-profile id** (such as one starting with `global.` or `us.`). A bare foundation-model id is rejected for on-demand use.

Each run records the model in `jobs/<job>.manifest.json`, so results stay traceable.

## `--reasoning`

The protocol asks for reasoning effort `xhigh` by default (`reasoning_effort` in `protocol.toml`); `--effort high` chooses the other listed level (`reasoning_efforts`). Runs compare only at the same effort, and the job manifest records it. A provider rejects a level its model does not support, so `check --model <model-id> --reasoning <style>` before a run confirms that the model accepts it. Model families take that setting in different ways, and `--reasoning` tells the harness which way to use.

| style | what is sent | typical model families (via LiteLLM) |
| --- | --- | --- |
| `effort` (default) | LiteLLM's top-level `reasoning_effort` | Anthropic Claude on Bedrock (sent as adaptive thinking plus effort); OpenAI models on Bedrock |
| `openai` | `llm_kwargs.reasoning.effort`, sent to the provider as a request field | models whose provider takes a `reasoning` object in the request body |
| `none` | nothing; the model uses its own default | models without a reasoning-effort setting |

**Always check before a full run.** `check --model <model-id> --reasoning <style>` sends one short request with exactly the settings a run uses. If the provider rejects a parameter, the check fails instead of every trial.

Comparable scores need the same style across runs of one model. The style is recorded in the job manifest.

## Profiles

A profile chooses the LLM route and the sandbox. `profiles/bedrock-docker.toml` uses Amazon Bedrock for the LLM and local Docker for sandboxes:

```toml
[llm]
route = "bedrock"                         # LiteLLM provider prefix
api_key_env = "AWS_BEARER_TOKEN_BEDROCK"  # optional; otherwise the AWS credential chain is used
api_key_aliases = ["AMAZON_BEDROCK_API_KEY"]
api_key_required = false

[llm.env]
AWS_REGION = "us-east-1"
AWS_REGION_NAME = "us-east-1"

[sandbox]
environment = "docker"
```

The region is us-east-1 unless `AWS_REGION` or `AWS_REGION_NAME` is already set in the environment; `AWS_REGION_NAME` has higher priority. A region set only in `~/.aws/config` is not used. Each job manifest records the profile's environment as the run saw it (`env`), so the region is on record.

To use another LiteLLM provider, add a file such as `profiles/my-provider.toml`. Set `route` to the provider's LiteLLM prefix, `api_key_env` to the environment variable that holds its key, and `api_key_required = true`. Then pass `--profile my-provider` and the model id on that provider.

The protocol settings in `protocol.toml` apply unchanged to every profile, so scores from different profiles stay comparable.

# The same run with `harbor run`

`swecloud run` builds a single `harbor run` command from `protocol.toml`, the profile and the preset. Print it for the user's exact choices and run that, rather than writing it by hand:

```bash
uv run --locked swecloud run --profile <profile> --split <split> --model <model> \
    --reasoning <style> --n-concurrent <n> --job-name <name> --dry-run
```

For `bedrock-docker`, issue-fixing, `--reasoning effort`, it prints (paths shortened):

```bash
harbor run --job-name <name> --jobs-dir $PWD/jobs --path $PWD/tasks/issue-fixing \
  --n-attempts 3 --n-concurrent <n> --max-retries 1 --timeout-multiplier 1.0 \
  --env docker --agent swecloud.agents.terminus:Terminus2 \
  --model bedrock/<model> \
  --ak record_terminal_session=false --ak reasoning_effort=xhigh \
  --ak 'llm_kwargs={"timeout":1200,"max_tokens":128000}' --yes
```

With `--reasoning openai` the effort moves into `llm_kwargs` as `"reasoning":{"effort":"xhigh"}` and the `reasoning_effort` argument is dropped; with `none` both are dropped. The oracle preset is `--agent oracle --n-attempts 1` with no model arguments.

Before running it directly:

1. Run it as `uv run --locked harbor run ...` so Harbor finds the protocol harness (`swecloud.agents.terminus`).
2. Export what `swecloud` would otherwise set: the variables in `.env`, the profile's `[llm.env]` (`AWS_REGION` and `AWS_REGION_NAME=us-east-1` for Bedrock), and the protocol's settings from `protocol.toml` `[env]`: the keepalive settings (`AIOHTTP_SO_KEEPALIVE=true`, `AIOHTTP_TCP_KEEPIDLE=30`, `AIOHTTP_TCP_KEEPINTVL=30`, `AIOHTTP_TCP_KEEPCNT=5`), and `HARBOR_TELEMETRY=0`, which turns off Harbor's usage telemetry. Without the keepalive settings, long LLM requests can be dropped.
3. Keep `--jobs-dir` absolute.

A direct run writes no `jobs/<name>.manifest.json`; `swecloud score jobs/<name>` still works on its job folder.

# Rebuild a state machine and schedule sync tool

`/reference/program` is a command-line tool that keeps AWS Step Functions state machines and
their EventBridge Scheduler schedules in line with a Jsonnet configuration. It has no state
file. Each run compares the config with what AWS reports, then prints a plan or applies it.
`plan` shows per-resource text diffs, an operation summary and an optional JSON report.
`apply` creates or updates resources, reconciles tags and honours the `deleteSchedule` /
`deleteAll` flags. `import` writes existing resources into a config file.

Build your own implementation of this program. Write the source under `/app` plus an
executable `/app/build.sh` that produces `/app/program`. Any installed language is fine;
Python with boto3 and the `_jsonnet` module is available. Only `/app` is carried over to
grading. The reference, `/docs` and any AWS resources you create are not. The reference is
for running experiments, not for copying or decompiling.

Start with `/docs/interface.md`: invocation, AWS endpoint, config format, scope, and how
outputs and AWS state are compared. The guide is deliberately incomplete. Find out by
experiment how the reference renders and compares resources, which changes it reports and
how, how it treats fields that AWS fills in with defaults, how the deletion flags combine
with what exists remotely, and what is left behind when a run fails or is cancelled.

Grading runs your `/app/program` against a fresh AWS emulator at the same endpoint. It
checks exit status, stdout, the JSON report, imported config files and the resulting AWS
state.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

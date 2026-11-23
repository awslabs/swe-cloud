# Rebuild a sealed environment-secret manager

`/reference/program` is a command-line secret manager. A project config,
`fnox.toml`, declares providers backed by AWS — inline KMS ciphertext,
SSM Parameter Store parameters and Secrets Manager secrets — plus secrets,
profiles and STS lease backends. The program stores values (`set`, `import`),
resolves them (`get`, `export`, `env --json`, `exec`, `check`), rewrites
ciphertext (`reencrypt`), edits the config (`remove`) and mints short-lived
role credentials (`lease create/list/revoke`).

Build your own program that behaves the same way. Write the source under
`/app` and an executable `/app/build.sh` that produces `/app/program`. Only
`/app` is kept for grading, which runs in a fresh container with an empty AWS
endpoint. The reference, `/docs` and any AWS state you create are not
carried over. Any installed language is fine; Python with boto3 and the
usual shell tools are available.

Start with `/docs/interface.md`. It describes invocation, the AWS endpoint,
the admitted commands, config fields and local state, what is out of scope,
and exactly how results are compared. The guide is an orientation, not a full
specification. Settle the details by running the reference against the local
AWS endpoint: resolution order, defaults and `if_missing`, profile layering,
which file a write changes, how failures leave state, batching and lease reuse.
Use isolated `HOME` directories and fresh AWS resource names for your
experiments.

Run the reference only to observe its behavior. Do not copy, disassemble or
decompile it. Grading runs your `/app/program` as an unprivileged user and
checks exit statuses, outputs, child environments, config files, AWS state
and lease credentials.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

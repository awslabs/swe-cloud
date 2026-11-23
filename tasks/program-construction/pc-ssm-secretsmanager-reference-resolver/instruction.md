# Rebuild the reference expander

`/reference/program` is a command-line tool that resolves reference expressions such as
`ref+awsssm://app/db/host` or `ref+awssecrets://app/creds#/password` inside YAML/JSON
documents and strings. It fetches values from SSM Parameter Store, Secrets Manager and
KMS through the AWS endpoint configured in the environment (an AWS emulator at
`http://engines:4566`, dummy credentials, region `us-east-1`).

Write your own implementation of this program in `/app`, in any installed language.
Provide `/app/build.sh`; running it must produce the executable `/app/program`. Only
`/app` is carried over to a fresh grading machine. The reference program, `/docs` and
any AWS resources you create here will not be there, so the build must not depend
on them.

Start with `/docs/interface.md`. It describes invocation, the commands `eval`, `get`,
`env` and `exec`, the expression grammar, the three backends and their parameters,
what is in and out of scope, and exactly how output is compared. The guide is an
outline, not a full specification. Rules for map shaping, failure cases, type
preservation, rendering of embedded values, quoting and environment construction
have to be worked out by running `/reference/program` against resources you create
(Python with boto3 and PyYAML is available). Use the reference only by running it.
Do not copy, disassemble or decompile it.

Grading runs your `/app/program` against fresh SSM parameters, secrets and KMS keys
and compares standard output, exit status, and the resolved data or environment
with the reference's behaviour.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

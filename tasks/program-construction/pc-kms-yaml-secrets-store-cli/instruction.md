# Rebuild an envelope-encrypted secrets tool

`/reference/program` is a command-line secrets store. It keeps secrets in a
local YAML file. Each secret is encrypted locally with a fresh AWS KMS data
key, and the KMS-wrapped data key is stored next to the ciphertext. The tool
can also provision a KMS key for a label through CloudFormation, record that
key as the file's default, and later deprovision it. Build a program that
behaves the same way.

Read `/docs/interface.md` first. It covers the invocation and the endpoint
variables (the program reads `AWS_ENDPOINT`, not `AWS_ENDPOINT_URL`). It also
lists the commands and flags in scope, the exclusions, and how results are
compared. The guide only orients you. Learn the rest by running
`/reference/program` against the local AWS endpoint and inspecting what it does: the files it writes, its outputs and exit statuses,
and the KMS, CloudFormation and IAM state it leaves. Pay attention to failure
cases and to how commands interact over several steps. You may run the
reference, but do not copy or decompile it.

Write your source under `/app` along with an executable `/app/build.sh` that
produces `/app/program`. Only `/app` is carried over to a fresh grading
machine. The reference, the docs and any AWS state you create while exploring
are not. Any installed language is fine. Python 3 with boto3, PyYAML,
cryptography and PyNaCl is available, along with standard shell tools.

Grading runs `/app/program` as an unprivileged user against a fresh local AWS
endpoint and also checks the state left after failing commands.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

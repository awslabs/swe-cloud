# Rebuild schedule-rule-sync

schedule-rule-sync is a declarative command-line tool that keeps EventBridge
Rules and EventBridge Scheduler schedules in line with YAML or Jsonnet
configuration files. It validates configurations offline, dumps current AWS
state as configuration, prints unified diffs between AWS and the
configuration, applies the configuration and prunes resources it previously
managed. A working copy is installed at `/reference/program`. Run it freely
against the local AWS emulator to learn its behavior, but do not copy,
disassemble or decompile it.

Write your own implementation in `/app`. Provide `/app/build.sh`; when it runs
from `/app` it must produce the executable `/app/program`. You may use any
language installed in this environment; Python 3 with boto3, PyYAML and the
`_gojsonnet` module are available. Only the contents of `/app` are kept: the
grader builds and runs your program in a fresh environment against a fresh
emulator, without network access, without `/reference` and without any AWS
state you created.

The guides in `/docs` describe the command interface (`/docs/interface.md`)
and the configuration format (`/docs/configuration.md`). Read
`/docs/interface.md` for the invocation, the emulator endpoint, which
features and inputs are in scope, what is excluded, and exactly how outputs
and AWS state are compared. Where the guides are silent, the reference's
observable behavior is the specification: outputs, exit codes,
diagnostics, and the AWS resources, targets and tags left behind after each
command.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

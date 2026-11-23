# Rebuild the account sweeper

`/reference/program` is a command-line tool that cleans out one AWS account. It reads
a YAML configuration and refuses accounts that fail its safety checks. It then
discovers resources of the selected types, keeps the ones the configuration's filters
match, and reports or deletes the rest. Your job is to write your own program that
behaves the same way.

Start with `/docs/interface.md`. It covers how to invoke the program and point it at
the local AWS emulator (`http://engines:4566`), which resource types and features are
in scope, and how results are compared. The configuration language is described in
`/docs/configuration.md`. The guides leave many details out on purpose. Work them out by
running the reference against resources you create yourself, for example with boto3.
A fresh 12-digit access key ID gives each experiment its own emulator account. Explore
the identifiers and properties each type reports, how each filter option decides, how
groups, presets and global filters combine, and what a real run does when a deletion
is slow, held back or refused.

Put your source code in `/app`, along with an executable `/app/build.sh` that builds
`/app/program`. You may use any installed language. Only `/app` is carried over to a
fresh verifier. The reference, the guides and your emulator state are not. The
verifier runs your program against new accounts and checks its exit status, its report
of removed and kept resources, and the AWS state left behind.
Use the reference only by running it. Do not copy, disassemble or decompile it.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

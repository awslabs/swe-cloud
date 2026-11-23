# Rebuild the credential vault broker

`/reference/program` is a command-line credential vault for AWS. It keeps
long-term access keys in an encrypted file store and turns them into temporary
STS credentials for the profiles in an AWS config file. It can print those
credentials (`export`), run a command with them (`exec`), list and clear what it
has stored and cached, and rotate a stored key through IAM. A live STS/IAM
service is available at `http://engines:4566`.

Write your own implementation of this program, in any installed language, so
that `/app/program` behaves like the reference for the admitted scope. Put your
source under `/app` and an executable `/app/build.sh` that builds
`/app/program`. Only `/app` is kept. The build and the tests run in a fresh
container with a fresh STS/IAM service and without the reference, the
documentation, internet access or any AWS state you created.

Start with `/docs/interface.md`: invocation, venue, config keys and variables
in scope, exclusions and the exact output comparison rules. It describes the
surface only. How profiles are resolved, which STS call is made and when a cached
session is reused, how durations are chosen and capped, what the `exec` child
receives, and what `rotate` changes are for you to learn by running the
reference against the live service. Use real IAM users, keys and roles, and
compare your program with it command by command.

Use the reference only by running it. Do not copy, disassemble or decompile it.
The tests create their own IAM users and roles, config files and stores, and
check outputs, the cached-session listing and IAM state independently.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

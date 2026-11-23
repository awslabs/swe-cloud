Build a job server that behaves like the reference program at `/reference/program`.

The reference is a long-running TCP server. Clients submit jobs for named
functions, workers that registered those functions fetch and finish them, and
administrators query it with a line-based text protocol on the same port.
Background jobs are kept durable in a MySQL table, an Amazon RDS instance in the
local AWS emulator configured in your environment, so pending work survives a
restart.

Start with `/docs/interface.md`: command line, RDS setup, the queue table, admin
commands, scope and comparison rules. `/docs/protocol.md` describes the binary
packets in scope. No client library for this protocol is installed; write a small
client of your own to explore the reference.

The documents leave the important rules to you: which jobs are stored and how
their fields are encoded, when rows appear and disappear, how duplicate
submissions and unique ids behave, the order in which workers receive jobs, how
scheduled jobs wait, and what a restarted server does with the rows it finds.
Run the reference against your own RDS instance, watch packets and table, and
reproduce what you observe, including error replies and the state left behind
by refused requests.

Put your source in `/app` and write `/app/build.sh`, which must produce the
executable `/app/program` accepting the reference's invocation. Any installed
language may be used; Python with PyMySQL and boto3 is available. Only `/app` is
kept for grading, which runs in a fresh environment with its own database. Use
the reference only by running it; do not copy or decompile it.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

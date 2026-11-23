Build a program that reproduces the observable behaviour of `/reference/program`, a
streaming ingester that consumes JSON messages from a Kafka topic and appends them to an
existing Delta Lake table in S3, recording its Kafka progress inside the table's own
transaction log so that a later run continues where an earlier one stopped. Messages it
cannot ingest may be diverted to a second Delta table of dead letters.

Read `/docs/interface.md` first. It fixes the command syntax and the options in scope,
how the AWS emulator and the MSK-provided Kafka broker are set up, how tables are
created, what is observed and how results are compared. `/docs/delta-protocol.md`
describes the Delta log and checkpoint formats. Run the reference against topics and
tables you create to learn what it actually does: which commits it writes and when,
what each row and offset record contains, how it starts, resumes and ends, how
transforms and the table schema shape the data, and how it fails.

Write your source under `/app` together with an executable `/app/build.sh` that
produces `/app/program`. Only `/app` is carried over to the grading environment; the
reference, the documentation and any AWS or Kafka state you create are not. Use the
reference only by running it, not by copying or decompiling it. Any installed language
is allowed; Python with boto3, `confluent_kafka`, `pyarrow` and `jmespath` is available,
as are standard shell tools. Your program is tested through its command line against fresh topics,
tables and lock tables, and must match the reference wherever results are observable.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

# Rebuild a Kafka stream pipe tool

`/reference/program` is a command-line tool that moves data between delimited
text and Apache Kafka topics. It produces records from stdin or files, consumes
them through a small output format language or a JSON envelope, joins balanced
consumer groups and lists cluster metadata. Write your own implementation that
behaves the same way.

Start with `/docs/interface.md`. It covers invocation, how to provision the
Amazon MSK cluster the tool talks to (through the local AWS endpoint), the
admitted options and modes, and the rules for comparing outputs. Some things
are left out on purpose: how input splits into keys and values, when keys and
values become NULL, how offsets and timestamp windows resolve, when consumers
stop, what gets committed, how transactions appear on the broker, and which
command lines fail. Learn these by running the reference against topics you
create. You may run the reference only to observe its behaviour. Do not copy,
disassemble or decompile it.

Put your source under `/app` with an executable `/app/build.sh` that creates
`/app/program`. Only `/app` moves to a fresh grading machine. The reference,
the docs and any cluster you created stay behind, and the build has no network
access. Any installed language is fine. Python with confluent-kafka and boto3
is available, as are the librdkafka and yajl C libraries.

Grading uses a new cluster. It checks stdout bytes, exit statuses, and broker
state read back with an independent Kafka client.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

# Rebuild the CloudWatch Logs → OpenSearch forwarder

`/reference/program` is a working log forwarder. It runs until signalled. It polls
log events from CloudWatch Logs, reaches CloudWatch and STS through the venue's AWS
endpoint, and indexes each event as a document in an OpenSearch cluster. A YAML
configuration chooses the log groups and streams to read, where reading starts, and
whether the read position is persisted between runs. It also chooses how documents
are shaped and which index each one lands in, and how failed exports are retried.

Build your own implementation, in any installed language, that behaves the same way.
Put the source under `/app` and write `/app/build.sh`; it must produce the executable
`/app/program`. Only `/app` carries over to a fresh grading environment. The reference,
`/docs` and any AWS or OpenSearch state you create during development do not.

Start with `/docs/interface.md`. It covers invocation, AWS and OpenSearch setup, the
admitted configuration, exclusions and exactly how results are judged.
`/docs/configuration.md` lists the configuration keys. The guides only describe the
surface. To learn the actual rules, create log groups and events, provision an
OpenSearch domain, and run the reference against them. Find out exactly which events
each poll forwards, what survives a restart, how index names are formed, and what
happens when OpenSearch rejects documents. Then compare the documents your program
writes with the reference's. You may run the reference as much as you like, but do not
copy, disassemble or decompile it.

Python with boto3 and opensearch-py is available, as are standard shell tools.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

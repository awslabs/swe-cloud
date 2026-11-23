# Rebuild bucket-event-indexer

`/reference/program` is a working build of `bucket-event-indexer`. It is a long-running
log shipper: it polls an SQS queue for S3 object notifications, downloads each announced
object, decodes it into events and indexes them into an OpenSearch domain through the
bulk API. It deletes each notification only once the outcome of its events is known.
It also validates its configuration files. Your job is to write your own program that
behaves the same way for the documented configuration surface.

Read `/docs/interface.md` first. It covers invocation and the configuration options in
scope, how to reach the AWS emulator and provision OpenSearch, what is excluded, and how
results are observed and compared. The guide deliberately leaves out exact consequences:
how fields are formed, which compression is chosen, what happens to malformed or failing
notifications, and how sink errors, retries and acknowledgements affect the queue. Find
these out by running the reference against real buckets, queues and domains. Treat
the reference as a black box to execute. Do not copy, disassemble or decompile it.

Put your source under `/app` together with an executable `/app/build.sh`. Running it
must produce the executable `/app/program`. Any installed language is fine, and
Python with boto3 and standard shell tools are available. Only `/app` is carried to a
fresh grading machine. The reference, the docs and any AWS or OpenSearch state you
create stay behind, so `/app/program` must not depend on them. Grading runs
`/app/program` as an unprivileged user against fresh resources, with configurations
written in the documented syntax.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

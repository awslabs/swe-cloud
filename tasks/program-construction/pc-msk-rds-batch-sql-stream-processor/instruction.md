# Rebuild a SQL stream processor

`/reference/program` is a streaming pipeline runner. It reads JSON records
from Kafka in batches and runs a DuckDB SQL statement over each batch. It
writes the result rows to Kafka, a Postgres table or standard output, and it
can aggregate rows into tumbling event-time windows. It also checks pipeline
configuration files offline with `validate` and `config validate`. Build your
own program that behaves the same way.

Start with `/docs/interface.md`. It covers the invocation, the configuration
schema, the AWS venue (Kafka through Amazon MSK, Postgres through Amazon RDS,
both at `AWS_ENDPOINT_URL`), the admitted scope and exactly how results are
compared. The guide names the settings but leaves most behavior unstated, so
run the reference program to learn it. Things to establish include where
batches begin and end, what is committed and when, how result rows become
records and table rows, what each error policy does, how windows close, and
what each report says. DuckDB v1.5.2 (C library, header and Python
binding), confluent-kafka, psycopg, PyYAML, Jinja2 and boto3 are installed.
You may use any installed language. Use the reference only by running it.
Do not copy, disassemble or decompile it.

Put your source in `/app` with an executable `/app/build.sh`. A fresh
verifier copies only `/app`, runs `/app/build.sh` as an unprivileged user and
then tests `/app/program` against new MSK and RDS resources. Neither the
reference program, the docs nor any AWS state you create is available
there. Your program must not call the AWS API itself. Like the reference, it
connects only to the brokers and DSNs that its configuration names.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

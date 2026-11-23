# Rebuild a lease-balanced Kinesis bridge

`/reference/program` runs a YAML-described stream pipeline. Its Kinesis input consumes the
shards of one or more streams and coordinates with other instances through a DynamoDB lease
table. For each shard, that table records the owner, the lease expiry and the last fully
acknowledged sequence. Its Kinesis output writes messages to a stream. Build your own program
with the same observable behaviour.

Read `/docs/interface.md` first. It covers the graded invocation (`run CONFIG.yaml`), the
emulator endpoint, the admitted components, fields and YAML subset, and exactly how outputs,
table items and records are compared. `/docs/components.md` is the field reference.

The guides do not describe the lease protocol. That includes when shards are claimed, stolen,
yielded or taken over from stopped or crashed instances, and what each table item looks like at
each point. It also covers how acknowledgements, batching and shutdown move the stored sequence.
Work these out by running the reference against the local emulator, with several instances and
with table items you write yourself. Python with boto3 and the usual shell tools are available.
You may use the reference only to run it, not to copy or decompile it.

Put your source in `/app` with an executable `/app/build.sh` that produces `/app/program`.
Any installed language may be used. Only `/app` is carried over to a fresh grading machine,
where `/app/build.sh` runs first. The reference, the docs and your AWS state are not carried
over.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

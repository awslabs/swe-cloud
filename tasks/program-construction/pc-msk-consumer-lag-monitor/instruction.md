# Reconstruct the consumer-lag status service

`/reference/program` is a long-running service that watches a Kafka cluster
(here an Amazon MSK cluster served by the local AWS endpoint). It polls topic
end offsets, reads consumer-group offset commits from the broker's internal
offsets topic, keeps a sliding window of commits per group and partition, and
answers HTTP/JSON queries about topics, groups and each group's health
(`OK`, `WARN`, `ERR`, `STOP`, `STALL`, `REWIND`, `NOTFOUND`).

Build your own implementation that behaves the same way. Write the source under
`/app` and an executable `/app/build.sh` that produces `/app/program`. Only
`/app` is kept for grading: the build runs in a fresh environment with the same
installed tools, without the reference, the docs or any AWS/Kafka state you
created. Any installed language may be used. Python with `boto3`,
`confluent-kafka` and `requests` is available.

Read `/docs/interface.md` first. It covers invocation, configuration, MSK
setup, the HTTP routes and result shapes, what is in and out of scope, and how
results are compared. The guide does not spell out the evaluation rules,
window bookkeeping, filtering, expiry, caching or startup replay. Work those out
by running the reference against your own MSK cluster, topics and commits.

Use the reference only by running it and observing its behaviour. Do not copy,
disassemble or decompile it. Grading starts your program with generated
configurations, drives a real MSK cluster with ordinary Kafka clients, and
compares the HTTP results and process behaviour with the reference's.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

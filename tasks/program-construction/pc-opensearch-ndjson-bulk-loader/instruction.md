# Rebuild an NDJSON bulk loader for OpenSearch

`/reference/program` is a command-line tool that streams newline-delimited JSON
documents from a file or standard input into an OpenSearch index through the bulk
API, using parallel workers. Your job is to build your own program with the same
observable behaviour.

Read `/docs/interface.md` first. It describes the invocation, the flags that are
in scope, how to create an OpenSearch domain in this AWS environment, and exactly
how results are compared. Then run the reference against a domain you create and
study what it does: which documents end up in the index and with which ids, what
happens to existing documents, mappings and index settings, how invalid or
unusable input is handled, how batches and workers interact with failures, and
what it reports on standard error and through its exit status. Many of these
rules are only visible through experiments, so test edge cases rather than
relying on the guide alone.

Write your source code under `/app` together with an executable `/app/build.sh`
that produces `/app/program`. Only `/app` is carried over to a fresh grading
machine; the reference, the docs and any OpenSearch domains or indexes you
create are not. Any installed language is fine (Python with boto3 and
`opensearch-py` is available). Use the reference only by running it — do not
copy, wrap or decompile it.

Grading runs `/app/program` against a fresh OpenSearch domain and compares the
index contents, index settings, exit status and diagnostics with the reference.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

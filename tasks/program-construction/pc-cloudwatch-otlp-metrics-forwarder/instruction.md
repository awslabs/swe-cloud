# Rebuild an OTLP-to-CloudWatch EMF metrics forwarder

`/reference/program` is a long-running agent. It receives OpenTelemetry (OTLP)
metrics over HTTP (protobuf or JSON) and gRPC. It forwards them to CloudWatch
Logs as Embedded Metric Format (EMF) JSON log events. Your job is to write your
own implementation that behaves the same way. Your program must be
`/app/program`, built by `/app/build.sh`.

Start with `/docs/interface.md`. It covers invocation, the AWS endpoint and
credentials, the options in scope (including their environment-variable
forms), and the rules used to compare your results with the reference's.
`/docs/emf-format.md` summarizes the EMF document format. Run the reference
against the task's AWS endpoint and inspect what it writes. Many rules are not
written down, and you will need to discover them by experiment, for example:

- how data points are grouped into documents
- how the namespace and dimensions are formed
- how units are translated
- how gauges, sums, histograms and summaries are represented
- how cumulative data changes across exports
- how the log group, log stream and retention are managed
- how bad input is handled

Only `/app` is carried over to a fresh grading environment. The reference
program, `/docs` and any AWS state you create are not. The grader prepares or removes
the log groups for each test, starts your program and sends OTLP requests. It then
reads the resulting log events and log group state through the AWS API.

Use the reference only by running it. Do not copy, disassemble or decompile
it. You may use any installed language. Python 3 with boto3, the
OpenTelemetry protobuf bindings (`opentelemetry-proto`, `protobuf`, `grpcio`)
and standard shell tools are available.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

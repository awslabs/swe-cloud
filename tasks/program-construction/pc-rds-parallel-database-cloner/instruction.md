# Rebuild a parallel PostgreSQL database cloner

`/reference/program` is a command-line tool that copies one PostgreSQL
database into another: it dumps and restores the schema, copies table rows
with several concurrent workers that share one source snapshot, sets
sequences to their source values, builds indexes and constraints after the
data, and copies large objects. It can restrict what it copies with an INI
filter file, list what it would copy or skip, compare source and target, and
record its progress in a work directory so that a failed run can be resumed
without redoing finished work.

Write your own implementation, in any language available here, and a script
`/app/build.sh` that builds it into the executable `/app/program`. Only `/app`
is kept for grading; the reference, the documentation and any databases you
create are not. Use the reference only by running it: experiment with it
against RDS PostgreSQL instances in the local AWS emulator. Do not copy,
decompile or wrap it.

Read `/docs/interface.md` first. It describes the invocation, the
connection settings, how to create the RDS instances, which commands and
options are in scope, and exactly how results are observed. `/docs/filters.md`
describes the filter file format.

Grading runs your program against fresh source and target databases and
inspects the target with SQL, the exit statuses, and the text and JSON
reports on stdout. Which objects are copied, skipped or rebuilt, and how
repeated, failed and resumed runs behave, should match the reference.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

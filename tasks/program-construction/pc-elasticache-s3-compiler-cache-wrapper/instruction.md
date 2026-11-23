# Reconstruct build-cache

`/reference/program` is a compiler cache for C. Placed in front of a GCC command line
(`program gcc -c foo.c -o foo.o`), it hands the request to a background server, which
decides whether the compile can be cached and derives a cache key. On a hit it restores
the object file and the compiler's captured output. Otherwise it runs the compiler and
stores the result in a two-level chain: a Redis-protocol store (ElastiCache Valkey)
first, then an S3 bucket. The server reports detailed statistics.

Build your own implementation that behaves the same way. Put your source in `/app`
and write `/app/build.sh`, which must produce the executable `/app/program`. Only
`/app` is carried to a fresh grading machine: the reference, `/docs` and any AWS
resources you create will not be there. You may use any installed language.

Read `/docs/interface.md` first. It covers invocation, server and storage variables,
how to provision Redis and S3 against the local AWS endpoint, the cache-entry format,
and exactly what is graded and how statistics and storage are compared.

Then explore the reference by running it, and match what it actually does. Look in
particular at:

- which invocations it caches, and how it counts the ones it declines;
- which inputs change the cache key and which do not;
- the order of lookups, writes and backfills across the two levels, and the per-level
  counters;
- read-only levels, TTLs, unreachable or missing storage, and the write-error policy;
- server lifecycle and statistics reset.

Run the reference only to observe its behavior. Do not copy, disassemble or decompile
it.

The task image's language/toolchain versions and Python packages are recorded in /etc/task-runtime.json before agent setup. Extra runtimes or packages installed by the agent harness are not guaranteed in the verifier. Only /app transfers; build.sh and program run offline as candidate with PATH=/usr/local/bin:/usr/bin:/bin and the AWS endpoint settings of this environment. Use the task-provided tools or bundle additional dependencies under /app and select them explicitly.

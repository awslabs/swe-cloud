# build-cache: interface

build-cache is a compiler cache for C compilations. You put it in front of a compiler
command line. It decides whether the invocation can be cached and computes a cache key.
On a hit it restores the compiler's outputs (object file, captured stdout/stderr) from
remote storage. Otherwise it runs the compiler and stores the result. In this
environment storage is a two-level chain: a Redis-protocol store (Amazon ElastiCache,
Valkey) first, then an S3 bucket.

The reference executable is `/reference/program`; your build must produce
`/app/program` with the same observable behavior.

## Invocation

The executable is invoked directly (it is not on `PATH`):

```
program gcc -c src/foo.c -o foo.o        # wrap one compiler invocation
program --start-server                   # start the background server
program --stop-server                    # stop it (prints final statistics)
program --show-stats [--stats-format json|text]
program --zero-stats                     # reset statistics counters
program --version | --help
```

* `program <compiler> <args...>` runs the compile through the background server and
  auto-starts the server if none is running. The process exit status, stdout and
  stderr are the compiler's (replayed on a cache hit). The compiler is located through
  `PATH`, or given as a path.
* Invocations the cache cannot or does not store still run the compiler and behave as
  the plain compiler would; the statistics record why they were not cached.
* Only GCC-style C compilation (`gcc`, `cc` and other names/paths of the same GCC) is in
  scope. Rust, CUDA, MSVC, C++, assembler sources, other front ends and distributed
  compilation are not.
* Other options listed by `--help` (distributed compilation, toolchain packaging,
  preprocessor-cache debugging, advanced statistics) are outside the admitted scope.
* Failures of the program itself (no server to stop, a server already listening on the
  port, invalid storage configuration, an unknown or unsupported compiler) exit with a
  nonzero status and a diagnostic on stderr.

## The server

All caching happens inside a background server process that clients reach over TCP on
`127.0.0.1:$SCCACHE_SERVER_PORT` (default 4226). The server reads the storage
configuration below **from its own environment when it starts**, and refuses to start
when that configuration is unusable. Storage settings seen only by later client
invocations do not reconfigure a running server. To change configuration, stop the
server and start it again with the new environment.

Statistics are kept in the server's memory. They survive across compilations, reset
with `--zero-stats` and are lost when the server stops.

| Variable | Read by | Meaning |
|---|---|---|
| `SCCACHE_SERVER_PORT` | both | TCP port of the server (use a unique port per isolated run) |
| `SCCACHE_IDLE_TIMEOUT` | server | seconds of inactivity before the server exits; `0` disables |
| `SCCACHE_ERROR_LOG` | server | file receiving server diagnostics |
| `SCCACHE_LOG` | both | log filter, e.g. `debug` |
| `SCCACHE_RECACHE` | compiling client | when set, ignore existing entries for this compile and recompile/rewrite |
| `SCCACHE_C_CUSTOM_CACHE_BUSTER` | compiling client | arbitrary value mixed into the cache key of this compile |

## Storage configuration

```
SCCACHE_MULTILEVEL_CHAIN=redis,s3     # order is lookup order: L0 = redis, L1 = s3
SCCACHE_MULTILEVEL_WRITE_ERROR_POLICY=ignore|l0|all   # optional, default l0:
                                       # which levels' storage errors are fatal

SCCACHE_REDIS_ENDPOINT=redis://HOST:PORT
SCCACHE_REDIS_KEY_PREFIX=some/prefix/  # namespace for all keys written/read
SCCACHE_REDIS_TTL=SECONDS              # expiration applied to written keys
                                       # (SCCACHE_REDIS_EXPIRATION is the newer name;
                                       #  set at most one of the two)
SCCACHE_REDIS_DB=N                     # optional, default 0
SCCACHE_REDIS_RW_MODE=READ_ONLY|READ_WRITE

SCCACHE_BUCKET=bucket-name             # existing S3 bucket
SCCACHE_ENDPOINT=$AWS_ENDPOINT_URL     # S3 endpoint URL
SCCACHE_REGION=us-east-1
SCCACHE_S3_KEY_PREFIX=prefix/          # optional
SCCACHE_S3_RW_MODE=READ_ONLY|READ_WRITE
```

S3 credentials come from the standard `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
variables. A chain may name one or both levels, in either order. Without
`SCCACHE_MULTILEVEL_CHAIN`, a single configured backend is used on its own. Prefixes
used by the tests always end in `/`.

## AWS environment

`AWS_ENDPOINT_URL` points at an AWS-compatible endpoint (`us-east-1`, dummy
credentials). Create the resources with the AWS API before starting the server:

* S3: `create_bucket`, then pass the bucket name as `SCCACHE_BUCKET` and the endpoint
  URL as `SCCACHE_ENDPOINT`.
* Redis: `elasticache.create_replication_group(ReplicationGroupId=..., ReplicationGroupDescription=..., Engine="valkey")`.
  Wait until `describe_replication_groups` reports `Status == "available"`, then use
  `NodeGroups[0].PrimaryEndpoint.Address`/`Port` as `redis://ADDRESS:PORT`. The cache
  engine runs on the emulator host. If the reported address is `localhost` or
  `127.0.0.1`, connect to the hostname of `AWS_ENDPOINT_URL` (e.g. `engines`) on the
  reported port instead. This applies both to `SCCACHE_REDIS_ENDPOINT` and to any Redis
  client used for inspection. No authentication is configured.

Python with `boto3`, `redis` and `zstandard`, the `zstd` command and `libzstd` are
installed for exploration and for your implementation.

## Observation

Tests grade statistics, storage state and compiler outputs; they do not grade log
output, durations, timing or the wording of the program's own diagnostics. Compiler
output is different: files written, stdout, stderr and exit status of a wrapped
invocation are compared byte for byte with the plain compiler run the same way (and,
on a hit, with what the original compile produced).

* **JSON statistics.** `--show-stats --stats-format json` prints one JSON object. Its
  `stats` member includes the counters `compile_requests`, `requests_not_compile`,
  `requests_not_cacheable`, `requests_unsupported_compiler`, `requests_executed`,
  `compilations`, `compile_fails`, `cache_writes`, `cache_write_errors`,
  `cache_read_errors` and `forced_recaches`; `not_cached` (reason → count);
  `cache_hits` / `cache_misses` (each `{"counts": {"C/C++": n}, ...}`, where an absent
  language key means 0); and `multi_level`, a list with one object per level in chain
  order (`name` such as `"L0 (redis)"` or `"L1 (s3)"`, and the counters `hits`, `misses`,
  `writes`, `write_failures`, `backfills_from`, `backfills_to`). Top-level members
  include `cache_location` (e.g. `"Multi-level (2 levels)"`), `version` and
  `effective_cache_mode` (`"ReadWrite"` or `"ReadOnly"`). Counters compare by numeric
  value (`1` and `1.0` are equal; booleans are not counts). `not_cached` reason keys are
  compared verbatim, as the reference reports them. Other members and duration fields
  are informational.
* **Text statistics.** The default text report (also printed by `--stop-server`) has
  one `Label   value` row per statistic; tests read the `Compile requests`,
  `Cache hits`, `Cache misses` and `Compilations` rows.
* **Entries.** Redis keys live under `SCCACHE_REDIS_KEY_PREFIX` and S3 object keys
  under the bucket/`SCCACHE_S3_KEY_PREFIX`. The key *names* are hashes that a different
  implementation is not expected to reproduce. Observable state is which entries exist,
  their count, their TTL, their decoded contents and what a later compile restores. Each
  entry value is a ZIP archive whose members (compiler outputs and any captured
  `stdout`/`stderr`) use the *stored* method (0) and each hold one zstd frame; tests decode members and look for the
  produced output files among them, and compare entries across levels by decoded
  members rather than raw bytes. A small internal health-check key whose final path
  component starts with `.` may appear under each writable level's prefix and is not
  counted as an entry. A read-only level is never written.
* Write timing. With a single backend (no chain), and for a chain under the `l0`
  (default) and `all` policies, a compile that stores an entry does not return until
  the backend's or first level's write has finished and `cache_writes`
  (or `cache_write_errors`) has been counted, so tests read the first level and the
  global counters as soon as the compile returns. Writes to later levels, every write
  under the `ignore` policy, backfills and the per-level counters they update may finish
  shortly after the client returns; tests poll briefly for those before concluding that
  an entry or count is absent.

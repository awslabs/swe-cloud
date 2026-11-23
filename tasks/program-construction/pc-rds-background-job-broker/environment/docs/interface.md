# background-job-broker: interface

`background-job-broker` is a job server. Clients submit jobs for named
functions, workers that registered those functions receive and complete them, and
background jobs are kept durable in a MySQL table so that they survive a server
restart. Clients, workers and administrators all talk to the same TCP port: binary
framed packets (see `protocol.md`) and a line-based text admin protocol.

## Invocation

The program is a long-running server in the foreground. It is started with the
MySQL persistent queue selected:

```
program -q MySQL \
    --mysql-host HOST --mysql-port PORT \
    --mysql-user USER --mysql-password PASSWORD \
    --mysql-db DATABASE --mysql-table TABLE \
    -p LISTEN_PORT [-L LISTEN_ADDRESS] \
    [--log-file FILE|stderr|none] [--pid-file FILE] [--verbose LEVEL] \
    [--job-handle-prefix PREFIX]
```

Options in scope:

| option | meaning |
| --- | --- |
| `-q`, `--queue-type MySQL` | select the MySQL persistent queue (`--queue-type=MySQL` form also accepted) |
| `--mysql-host` | MySQL server host name (default `localhost`) |
| `--mysql-port` | MySQL server port (default 3306) |
| `--mysql-user`, `--mysql-password` | MySQL credentials |
| `--mysql-db` | database that holds the queue table (must already exist) |
| `--mysql-table` | queue table name (default `gearman_queue`) |
| `-p`, `--port` | TCP port for clients, workers and admin connections (default 4730) |
| `-L`, `--listen` | address to listen on (default: all addresses) |
| `-l`, `--log-file` | log destination; default is a file under `/var/log`, which an unprivileged user cannot write. Use a writable path, `stderr` or `none`. |
| `-P`, `--pid-file` | pid file location (default is a file under `/var`); give a writable path |
| `--verbose` | log level: FATAL, ALERT, CRITICAL, ERROR, WARNING, NOTICE, INFO, DEBUG |
| `--job-handle-prefix` | prefix of generated job handles (default `H:<hostname>`) |
| `-h`, `--help` | list all options |

Both `--name value` and `--name=value` spellings work. The server runs in the
foreground until it receives SIGTERM or SIGINT, then shuts down cleanly with exit
status 0. It is ready once it answers on the listen port. If the MySQL connection
or the queue table setup fails at startup, the server prints an error on standard
error and exits with a nonzero status instead of serving.

Other queue types, SSL, the HTTP protocol module, daemon mode, job retry limits,
queue size limits and the thread tuning options are outside the task scope.

## MySQL / AWS setup

The MySQL server is an Amazon RDS MySQL instance in the local AWS emulator
(`AWS_ENDPOINT_URL`, region `us-east-1`, dummy credentials are preset). For example:

```python
import boto3
rds = boto3.client("rds")
rds.create_db_instance(DBInstanceIdentifier="jobs-db-1", Engine="mysql",
                       DBInstanceClass="db.t3.micro", AllocatedStorage=20,
                       MasterUsername="jobs", MasterUserPassword="jobs-pass-1",
                       DBName="jobs")
# wait until DBInstanceStatus == "available", then use
# DBInstances[0]["Endpoint"]["Address"] / ["Port"] as --mysql-host / --mysql-port
```

The engine may need a few more seconds after `available` before it accepts logins;
retry the first connection. Make sure the database named by `--mysql-db` exists
(`CREATE DATABASE IF NOT EXISTS ...`). PyMySQL is installed for inspecting tables.

## The queue table

When the table named by `--mysql-table` does not exist, the server creates it at
startup. Its columns are:

```
unique_key     the job's unique id
function_name  the function the job was submitted for
priority       the job's priority (an integer column)
data           the job's opaque workload (a binary BLOB column)
when_to_run    the scheduled run time of an epoch job (an integer column)
```

with a unique key over `(unique_key, function_name)`. An existing table that has
at least these columns is used as is, whatever its other columns or column order;
the server reads and writes only these five. An existing table that lacks one of
them is a table setup failure. Which jobs produce rows, when rows
appear and disappear, the stored value encodings, and how rows are loaded back
after a restart (including rows written by someone else) are part of the
observable behaviour to reproduce; inspect the table (for example with PyMySQL)
while jobs are queued, running and finished.

## Text admin protocol

On the same port, a connection whose first byte is not NUL is treated as text
commands, one per line terminated by `\n`; one connection may send several
commands. Commands in scope:

- `status` — one line per known function:
  `FUNCTION\tTOTAL\tRUNNING\tAVAILABLE_WORKERS`, then a line containing `.`.
  TOTAL counts the function's jobs held by the server, RUNNING those currently
  assigned to a worker, AVAILABLE_WORKERS the connected workers that registered
  the function. A function becomes known when a job is submitted for it or a
  worker registers it, and stays listed for the lifetime of the server process.
- `workers` — one line per open connection (clients, workers and the admin
  connection itself): `FD IP-ADDRESS CLIENT-ID : FUNCTION ...`, then a line
  containing `.`. The client id is `-` unless the connection sent
  `SET_CLIENT_ID`; the function list is empty for connections that registered
  none (`FD IP-ADDRESS CLIENT-ID :`).

Other admin commands are outside the graded scope.

## Scope bounds

- Unique ids of up to 63 bytes are in scope, as are over-long unique ids of more
  than 64 bytes (which are refused); a unique id of exactly 64 bytes is not graded.
- `SUBMIT_JOB_EPOCH` times are decimal integers from 0 to 2147483647.
- Workloads up to a few megabytes are in scope.
- A foreground client that disconnects while its submission is joined to a stored
  background job is not graded.

## Observation and equivalence rules

- Jobs, rows and admin counts are observed through the documented port and the
  MySQL table only; log text is not part of the interface.
- Job handles are opaque non-empty strings returned in `JOB_CREATED`; only their
  identity (same/different job) and their use in later packets matter, except
  that with `--job-handle-prefix PREFIX` every handle starts with `PREFIX:`.
- The contents of the log file and pid file are not graded.
- Queue rows are compared as a collection (row order is irrelevant).
  `unique_key` and `function_name` are compared as text, `priority` and
  `when_to_run` by integer value, `data` byte for byte; SQL NULL and an empty
  value are equivalent for an empty workload.
- `status` and `workers` lines are compared as collections: line order and the
  order of functions within a `workers` line do not matter. FD numbers only need
  to be decimal integers.
- `ERROR` packets are compared by their code argument only.
- Among jobs of the same priority level that were loaded from the table at
  startup, delivery order is not graded; every other ordering is.
- Epoch scheduling uses whole seconds of wall-clock time; timing is observed with
  a tolerance of a few seconds.

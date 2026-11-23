# tenant-connection-pools — interface

`program` is a PostgreSQL wire-protocol connection pooler. It listens for PostgreSQL
clients, authenticates them against statically configured **tenant pools**, and forwards
their SQL to real PostgreSQL servers (here: Amazon RDS for PostgreSQL in the AWS venue)
over a bounded set of reusable server connections. SQL is executed by the server; the
pooler does not evaluate it.

## Invocation

```
program [OPTIONS] [CONFIG_FILE]
```

* `CONFIG_FILE` — path of a TOML configuration file (also read from the `CONFIG_FILE`
  environment variable; defaults to `pgcat.toml` in the working directory).
* `-l, --log-level <LEVEL>` (env `LOG_LEVEL`, default `INFO`), `-F, --log-format
  text|structured|debug`, `-n, --no-color`, `-h, --help`, `-V, --version`.

The program runs in the foreground until it is signalled (SIGINT/SIGTERM). Logs go to
the standard streams; their wording and format are not part of the graded interface, but
the logging options must be accepted. `--help`/`--version` output is not graded.

## The AWS venue

An AWS-compatible endpoint is available at `$AWS_ENDPOINT_URL` (region `us-east-1`,
dummy credentials already set). Create a PostgreSQL server with the RDS API, wait until
it is `available`, and use the `Endpoint.Address` / `Endpoint.Port` it reports:

```python
import boto3
rds = boto3.client("rds")
rds.create_db_instance(DBInstanceIdentifier="tenants-1", Engine="postgres",
                       DBInstanceClass="db.t3.micro", AllocatedStorage=20,
                       MasterUsername="master", MasterUserPassword="master-pass-1")
rds.get_waiter("db_instance_available").wait(DBInstanceIdentifier="tenants-1")
ep = rds.describe_db_instances(DBInstanceIdentifier="tenants-1")["DBInstances"][0]["Endpoint"]
```

Connect to that endpoint directly (database `postgres`, master credentials) to create the
tenant roles, databases and tables you need; the program itself only connects with the
backend credentials you put in its configuration. Delete instances you create.

**Server addresses for the program.** In this venue, the reported endpoint is an
authentication relay that asks every client for a cleartext password. The program's
server-side login supports only the MD5 and SCRAM methods a PostgreSQL server uses, so
the `servers` entries in its configuration name the PostgreSQL engine behind the relay.
Read that address through the endpoint with ordinary SQL, then use it as host and port:

```sql
SELECT host(inet_server_addr()), inet_server_port();
```

The engine is the same RDS-created database: roles, databases and rows created through
the endpoint are the ones the program sees there.

`psql` (PostgreSQL 15 client) and `psycopg` 3 are installed for clients and inspection.

## Configuration file (admitted scope)

```toml
[general]
host = "127.0.0.1"          # listen address
port = 6432                 # listen port
admin_username = "admin"    # admin console login (see "Admin console")
admin_password = "admin-pass"
# connect_timeout = 1000    # milliseconds the pooler waits for a server connection

[pools.alpha]               # pool name = the database name clients connect to
pool_mode = "transaction"

[pools.alpha.users.0]
username = "app"            # frontend user name
password = "alpha-secret"   # frontend password (MD5 password authentication)
server_username = "tenant_a_owner"   # optional: backend login (defaults to username)
server_password = "a-backend-pw"     # optional: backend password (defaults to password)
pool_size = 2               # pool size for this pool/user

[pools.alpha.shards.0]
servers = [["<engine address>", <engine port>, "primary"]]
database = "tenant_a_db"    # physical database on the server

[pools.beta]
pool_mode = "transaction"

[pools.beta.users.0]
username = "app"
password = "beta-secret"
server_username = "tenant_b_owner"
server_password = "b-backend-pw"
pool_size = 3

[pools.beta.shards.0]
servers = [["<engine address>", <engine port>, "primary"]]
database = "tenant_b_db"
```

* A client selects a pool by the **database name** in its startup message together with
  its **user name**. The same frontend user name may appear in several pools, each with
  its own password, backend credentials, physical database and `pool_size`.
* A pool has one or more shards, `shards.0`, `shards.1`, ... Each shard's `servers` lists
  `[host, port, role]` entries with role `"primary"` or `"replica"` (at most one primary).
  With more than one server in a shard, `default_role` in `[pools.<name>]` (`"any"`,
  `"primary"` or `"replica"`) selects which of them may serve a client's transactions, and
  reads are spread across the eligible servers. Every server in a shard is expected to hold
  the same `database`; different shards may name different databases.
* `pool_mode` is `"transaction"` (a server connection is assigned to a client for the
  duration of a transaction, or a single statement outside one, and then returned for
  reuse) or `"session"` (a client keeps the server connection it first gets until it
  disconnects).

### Further settings in scope

* `[general]`: `connect_timeout`, `idle_timeout` and `server_lifetime` (milliseconds, server
  connections), `idle_client_in_transaction_timeout` (milliseconds), `shutdown_timeout`
  (milliseconds), `ban_time` (seconds), `autoreload` (milliseconds), `tls_certificate` and
  `tls_private_key` (PEM files for client TLS), `admin_auth_type` (`"md5"` or `"trust"`).
* `[pools.<name>]`: `pool_mode`, `default_role`, `load_balancing_mode` (`"random"` or
  `"loc"`), `idle_timeout`, `query_parser_enabled`, `query_parser_read_write_splitting`,
  `primary_reads_enabled`, `default_shard` (`"shard_<n>"` or `"random"`),
  `sharding_function` (`"pg_bigint_hash"`), `sharding_key_regex`, `shard_id_regex`,
  `automatic_sharding_key` (`"<table>.<column>"`), `prepared_statements_cache_size`.
* `[pools.<name>.users.<n>]`: `auth_type` (`"md5"` or `"trust"`), `min_pool_size`,
  `statement_timeout` (milliseconds).

The sharding function `pg_bigint_hash` is meant to place a bigint key on the same shard
index that PostgreSQL's own `PARTITION BY HASH` places it in a partition of the same
modulus. A configuration the program refuses (unknown enumerated values, inconsistent
server lists, invalid regular expressions, settings that need another setting, sizes
that contradict each other, ...) is observed only through the exit status and the
program never listening.

Out of scope: `auth_query`, plugins, mirrors, the Prometheus exporter, the DNS cache,
TLS towards the servers (`server_tls`), the `sha1` sharding function, health-check and
TCP keepalive tuning, worker threads, logging settings.

## Pooler commands

Outside a transaction, clients of a pool may send these commands as simple queries; the
pooler answers them itself (inside a transaction they are forwarded to the server as SQL):

* `SET SERVER ROLE TO '<primary|replica|any|auto|default>'`, `SHOW SERVER ROLE`
* `SET PRIMARY READS TO <on|off|default>`, `SHOW PRIMARY READS`
* `SET SHARD TO '<n>'`, `SET SHARDING KEY TO '<n>'`, `SHOW SHARD`
* with `shard_id_regex` / `sharding_key_regex` set, a query whose text matches the regex
  selects the shard (or the shard of the sharding key) it runs on.

Keyword case, a trailing semicolon and quotes around the value are accepted as the
reference accepts them, and are compared. Compared: the replies (columns, rows, command
tags, errors) and which server or shard database serves the client's later queries. With `query_parser_enabled` and
`query_parser_read_write_splitting`, which of a shard's servers serves a query depends on
what the query does; with `automatic_sharding_key`, writes that name the key column land
on the shard of their key. Both are compared through where rows and markers are found.

## Admin console

Connecting to the virtual database `pgcat` with `admin_username` / `admin_password` opens
the admin console. It accepts simple queries: `SHOW HELP|VERSION|DATABASES|POOLS|USERS|
CONFIG|LISTS|STATS|CLIENTS|SERVERS|BANS`, `SET <key> = <value>`, `PAUSE [<db>,<user>]`,
`RESUME [<db>,<user>]`, `BAN <host> <seconds>`, `UNBAN <host>`, `RELOAD` and `SHUTDOWN`.
Keyword case and a trailing semicolon are accepted as the reference accepts them.

Compared: column names and command tags; for listings, the rows' identifying and
configuration values (pool, user, server name, host, port, database, pool mode, sizes,
application name, client and server state and per-state counts, paused / disabled flags, ban rows and
durations) and the transaction and query counters after plain autocommit traffic; the
effects of PAUSE/RESUME, BAN/UNBAN, RELOAD and SHUTDOWN on clients; errors for unknown
or malformed commands. Not compared: row order, the text of `SHOW VERSION` and
`SHOW HELP`, ids, ages, byte counts, times and averages.

## Signals and reload

* SIGTERM stops the program at once.
* SIGINT starts a graceful shutdown: new clients are refused, clients are let go when they
  are idle, and the program exits when none are left or after `shutdown_timeout`.
* SIGHUP, the admin `RELOAD` command and `autoreload` re-read the configuration file;
  what is compared is which pools, users, passwords and sizes are in effect afterwards.

## Client and observation rules

* Connect with TLS disabled (`sslmode=disable`, `PGSSLMODE=disable`) unless the program
  is configured with `tls_certificate` / `tls_private_key`.
* Both the simple-query protocol (`psql -c`, psycopg with
  `cursor_factory=psycopg.ClientCursor`) and the extended protocol (psycopg's default
  parameterised cursors, prepared statements) are used.
* `BEGIN` / `COMMIT` / `ROLLBACK` and multi-statement query strings are forwarded as SQL.
* Cancel requests (a client's `BackendKeyData` sent on a new connection) are used.
* Results, error responses and connection refusals are observed as a PostgreSQL client
  sees them, with libpq/psycopg and with a plain socket speaking protocol 3.0 (startup
  message, `SSLRequest`, authentication requests, `PasswordMessage`, `Query`,
  `CancelRequest`, `Terminate`). Server-side effects (rows in each database, and
  `pg_stat_activity`: which backend user, database and state each server connection
  has, and its process id) can be observed by connecting directly to the RDS endpoint.
* Some tests point a server entry at an address where nothing listens.

## How behaviour is compared

* Everything the PostgreSQL server produces is compared as delivered to the client:
  result rows and column names, command tags, `NOTICE`/`WARNING` messages, error
  SQLSTATE, severity and primary message, and the transaction status carried by
  `ReadyForQuery`.
* Errors the pooler produces itself, at login or while serving a session, are compared
  by severity, SQLSTATE and when they happen: before or after the password exchange,
  and which query receives them, and whether the session survives them. Their message
  text, and anything in the program's logs, is not compared.
* Startup `ParameterStatus` values are compared only for `server_version` and
  `client_encoding`. `BackendKeyData` values and the MD5 salt only need to be fresh for
  each connection.
* The exit status of the program is compared when it cannot start from its
  configuration, and when it is stopped with SIGTERM or SIGINT.
* Server connections are counted per backend role in `pg_stat_activity`, together with
  their database and state (`idle`, `idle in transaction`, ...).
* Spreading over several servers or shards is random. Tests only require that, over a few
  dozen transactions, every eligible server is used and no other is. A transaction is
  served by one server from start to end.
* Timings are compared with tolerances of a few hundred milliseconds or more: a client
  being served without waiting for another tenant, waits bounded by `connect_timeout`,
  and the configured timeouts and lifetimes taking effect within a few seconds.

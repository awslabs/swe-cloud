# list-mailer: program interface

`list-mailer` is a self-hosted mailing-list and subscriber manager. It is a single
executable that keeps all of its state in a PostgreSQL database (here an RDS
Postgres instance in the AWS venue) and, when configured to, stores uploaded
media files in an S3 bucket. All behavior is reached through command-line
modes (database install/upgrade and a few utilities) and an HTTP JSON API
served by a long-running process. The reference executable is
`/reference/program`; your build must produce `/app/program`.

## Command line

```
program [--config FILE ...] [--install | --upgrade] [--yes] [--idempotent]
program --version
program --new-config [--config FILE]
```

| Flag | Meaning |
| --- | --- |
| `--config FILE` | TOML configuration file; may be repeated (merged in order). Default `config.toml` in the working directory. The file must exist. |
| `--install` | Create the database schema and seed data in the configured database, then exit. Existing tables of the program are dropped. Asks for confirmation on standard input unless `--yes` is given. |
| `--upgrade` | Apply pending schema migrations to an existing installation, then exit. |
| `--yes` | Assume "yes" for the interactive confirmation of `--install` / `--upgrade`. |
| `--idempotent` | With `--install`: do nothing if the database is already installed. |
| `--new-config` | Write a sample configuration file to the `--config` path and exit. |
| `--version` | Print the build version (`v6.2.0 ...`) and exit. |
| `--help` | Print flag help. |

Without `--install`/`--upgrade`/`--version`/`--new-config` the program runs the
HTTP server in the foreground until it receives SIGTERM/SIGINT. Diagnostic log
lines are written to standard output. The server refuses to start against a
database that has not been installed. Exit status 0 means success; any failure
exits non-zero.

## Configuration

The configuration file has an `[app]` section (listen address) and a `[db]`
section (Postgres connection). Only these keys are needed:

```toml
[app]
address = "127.0.0.1:9000"     # host:port the HTTP server listens on

[db]
host = "my-db.example"         # RDS endpoint Address
port = 5432                    # RDS endpoint Port
user = "app"
password = "secret"
database = "listmonk"          # must already exist on the server
ssl_mode = "disable"
max_open = 25
max_idle = 25
max_lifetime = "300s"
params = ""
```

Any key may also be supplied by environment variable: `LISTMONK_` followed by
the key path, with `__` separating sections, for example `LISTMONK_db__host`,
`LISTMONK_db__port`, `LISTMONK_app__address`. Environment values override the
file's values (the file itself is still required).

All other runtime settings (media storage, bounce handling, SMTP, privacy,
etc.) live in the database and are managed through the settings API (see
`api.md`). They are loaded at startup, so they survive restarts. A settings
update makes the running server reload itself in place; the reload must be
complete within two seconds of the update's response. Graders wait two
seconds and then poll `/api/health` before relying on the new settings.

## Installation and credentials

`--install` reads three optional environment variables:

| Variable | Effect |
| --- | --- |
| `LISTMONK_ADMIN_USER` | Username of a super-admin login user (min. 3 chars). |
| `LISTMONK_ADMIN_PASSWORD` | Password of that user (min. 8 chars). |
| `LISTMONK_ADMIN_API_USER` | Additionally create a super-admin API user of this name (requires the two above). |

When an API user is created, its random access token is written to standard
error as exactly one line:

```
export LISTMONK_ADMIN_API_TOKEN="<token>"
```

Installation also seeds a small set of example lists, subscribers, templates
and a campaign; inspect them after installing.

## Authentication

Every `/api/*` endpoint, including `/api/health`, and the bounce webhook
require credentials. Send either

```
Authorization: Basic base64(<api_user>:<token>)
Authorization: token <api_user>:<token>
```

Missing or wrong credentials are refused with an HTTP error status.
`GET /api/health` answers `{"data": true}` once the server is ready.

## AWS venue

* PostgreSQL: create an RDS instance (`Engine="postgres"`) through the AWS API
  at `AWS_ENDPOINT_URL`, wait until it is `available`, and put the returned
  `Endpoint.Address` / `Endpoint.Port` and master credentials in `[db]`. The
  database named in `[db] database` must exist (use `DBName` when creating the
  instance, or `CREATE DATABASE`). `psql` and Python `psycopg` are installed.
* S3 media: create a bucket, then switch the media provider to S3 through the
  settings API: `upload.provider = "s3"`, `upload.s3.url = <AWS_ENDPOINT_URL>`,
  `upload.s3.bucket`, `upload.s3.aws_default_region`,
  `upload.s3.aws_access_key_id`, `upload.s3.aws_secret_access_key`, and
  optionally `upload.s3.bucket_path`, `upload.s3.bucket_type`
  (`public`/`private`), `upload.s3.public_url`, `upload.s3.expiry`. Objects are
  addressed path-style (`<url>/<bucket>/<key>`).
* There is no mail server in the venue. Some operations (for example double
  opt-in subscriptions) try to send e-mail synchronously; graders point every
  SMTP server in the settings at `127.0.0.1` port `1` so these attempts fail
  fast. Such failures must not fail the operation. Mail delivery itself is out
  of scope.

## How graders run the program

Each check uses a fresh empty database on a shared RDS instance and a fresh
working directory containing `config.toml`. Graders run
`/app/program --install --yes --config config.toml` with the three admin
variables set, start `/app/program --config config.toml` as a background
process, wait for `/api/health`, use the API (plus direct reads of the
database and the S3 bucket), and finally stop the process with SIGTERM. Some
checks stop the server and start a new process on the same database. Commands
run as an unprivileged user with the AWS endpoint variables of the venue.

## Admitted scope

* `--install` (with and without `--yes`/`--idempotent`), `--upgrade`,
  `--version`, `--new-config`, configuration by file and environment.
* Lists: create, read, query, update, delete; public/private/temporary type;
  single/double opt-in; tags.
* Subscribers: create, read, update/patch, delete, blocklist, `attribs` JSON,
  list subscriptions and their statuses.
* Subscriber querying with an SQL expression over the `subscribers` table or a
  search string, and bulk actions by IDs or by query (list membership changes,
  blocklist, delete).
* CSV (optionally ZIP-compressed) subscriber import: `subscribe` / `blocklist`
  modes, overwrite switches, delimiter, target lists, subscription status,
  import status and logs.
* Bounces: recording via the bounce webhook, listing/deleting, blocklisting
  bounced subscribers, and the configured per-type count/action rules.
* Media upload to S3, listing, deletion; media URLs and image thumbnails.
* Settings read/update as they affect the above, including the privacy domain
  blocklist/allowlist and allowed upload extensions.

Out of scope: the web admin UI and any HTML pages, public subscription/archive
pages, OIDC and session login, users/roles management, POP3 bounce mailboxes,
provider-specific bounce webhooks (SES, SendGrid, ...), campaigns and
templates (beyond the seeded rows existing), campaign sending, transactional
mail, exports, the filesystem media provider.

## Observation rules

Results are judged on:

* HTTP status codes of the endpoints above, and their JSON envelope:
  `{"data": ...}` on success, `{"message": "<text>"}` on error. The message
  text is not compared.
* the JSON fields of response objects documented in `api.md`;
* rows in the program's Postgres tables, read with a Postgres client, using the
  reference schema's table and column names: `lists` (`id`, `uuid`, `name`,
  `type`, `optin`, `status`, `tags`, `description`), `subscribers` (`id`,
  `uuid`, `email`, `name`, `attribs`, `status`), `subscriber_lists`
  (`subscriber_id`, `list_id`, `status`), `bounces` (`id`, `subscriber_id`,
  `campaign_id`, `type`, `source`, `meta`, `created_at`), `media` (`id`,
  `uuid`, `filename`, `thumb`, `content_type`, `provider`, `meta`), `settings`
  (`key`, `value` as JSON), and the seeded `campaigns` rows (`id`, `uuid`,
  `name`). Enumerated columns are read as text (`status::text`), so either
  Postgres enums or text columns work; the values must be the reference's.
* S3 objects (keys, bytes, content type) written to the configured bucket, and
  the media URLs the API returns;
* the import status and counts reported by the import API;
* exit status and the API token line of the command-line modes.

Equivalences:

* JSON numbers compare by value (`1`, `1.0`, `1e0` are equal); booleans and
  `null` are distinct from numbers and strings. Object key order and
  whitespace are not significant.
* In the database, a `NULL` or empty `tags` array is equivalent, and a JSON
  `null` stored as a subscriber's `attribs` is equivalent to `{}`. API
  responses always give `tags` as an array.
* Generated values (ids from sequences, UUIDs, timestamps, API tokens, random
  name parts, presigned-URL signatures and dates) are not compared literally
  unless they follow from another observed value (for example a returned id
  must match the row, a supplied timestamp must be stored).
* List results are compared as collections unless the request gives
  `order_by`; then their order matters. `total`, `page` and `per_page` are
  compared.
* The per-list `subscriber_count`/`subscriber_statuses` fields are cached
  statistics and are not compared.
* Log text on standard output, HTML responses and the import log's text are
  not compared.
* Imports and bounce recording may complete after the request returns; graders
  poll for up to about 10 seconds.

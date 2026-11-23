# Scoped file sharing: interface

`program` is a multi-tenant file-sharing HTTP server. Local users sign in with a
password or an API token. They create *uploads* (containers of files), add files,
share download links and manage their own uploads. File bytes go to an S3 bucket and
all metadata goes to a PostgreSQL database. In this venue both are provided by the AWS
emulator: S3 directly, and Postgres through an RDS instance.

This guide gives the syntax, the admitted scope and how results are observed. Exact
status codes, messages and edge cases come from running the program.

## Invocation

```
program --config <file.toml>                 # run the HTTP server (foreground)
program --config <file.toml> --port <n>      # same, overriding ListenPort
program --config <file.toml> user  <create|list|show|update|delete> [flags]
program --config <file.toml> token <create|list|delete> [flags]
program --config <file.toml> file  <list|show|delete> [flags]
program --config <file.toml> clean
program --config <file.toml> export <output-file>
program --config <file.toml> import <input-file> [--ignore-errors]
program --help | <command> --help
```

The configuration file can also be passed through the `PLIKD_CONFIG` environment
variable. Administrative subcommands connect to the same metadata database (and, where
they remove data, the same bucket) as the server, and you can run them while the server
is up. Users are provisioned locally this way, for example:

```
program --config plik.toml user create --login alice --password 'alice-secret-1' --max-user-size 64B
program --config plik.toml user create --login bobby --password 'bobby-secret-2' --max-user-size 1KB
```

A local user's identifier is `local:<login>`. `--max-user-size` takes a human-readable
size (`64B`, `10MB`, ...) or `-1` for unlimited. If you leave it unset, the server-wide
`MaxUserSizeStr` applies. `user update --login L --max-user-size S` changes the limit
later. `token create --login L [--comment C]` mints an API token for a user, and
`token list --login L` and `token delete --login L --token T` manage tokens.

Other CLI scope:

* `user create` and `user update` also take `--name`, `--email`, `--password`,
  `--max-file-size <size>` (per-user file size limit), `--max-ttl <duration>` (per-user
  maximum TTL, e.g. `30m`, `2h`) and `--admin` (administrator account). `user show`
  prints the user as JSON; `user delete --login L` deletes a user and everything they
  uploaded.
* `file list [--upload U | --file F] [--human=false]` prints one line per file;
  `file show --file F` prints the file record; `file delete` takes exactly one of
  `--file F`, `--upload U` or `--all`.
* `user delete` and `file delete` ask for a `y/N` confirmation on standard input.
* `clean` runs one cleaning pass (see *Upload lifetime and cleaning*).
* `export` writes all metadata to a file; `import` loads such a file into another
  database. The file format is opaque; only the round trip is observed.

## Configuration (TOML)

Admitted configuration:

```toml
ListenAddress = "127.0.0.1"
ListenPort    = 8080
NoWebInterface = true                 # no browser UI is part of this task
FeatureAuthentication = "forced"      # authenticated uploads only (the only mode tested)
FeatureLocalLogin     = "enabled"
FeatureApiTokens      = "enabled"
FeatureRemovable      = "enabled"     # opt-in "anyone may delete" uploads
FeaturePassword       = "enabled"     # optional HTTP Basic protection of an upload's downloads
MaxUserSizeStr        = "unlimited"   # default per-user total size ("unlimited" or e.g. "1MB")
MaxFileSizeStr        = "10GB"        # per-file size limit
MaxFilePerUpload      = 1000
DefaultTTLStr         = "30d"         # upload lifetime when none is requested
MaxTTLStr             = "30d"         # "0" = no maximum
EnableArchiveCompression = true

# Other feature flags (each "disabled", "enabled" or "forced"):
#   FeatureOneShot, FeatureStream, FeatureComments, FeatureSetTTL,
#   FeatureExtendTTL, FeatureDeleteAccount
# FeatureRemovable, FeaturePassword and FeatureApiTokens take the same values.

DataBackend = "s3"
[DataBackendConfig]
    Endpoint        = "engines:4566"  # host:port taken from AWS_ENDPOINT_URL
    AccessKeyID     = "test"          # AWS_ACCESS_KEY_ID
    SecretAccessKey = "test"          # AWS_SECRET_ACCESS_KEY
    Bucket          = "my-bucket"     # created by the program if missing
    Location        = "us-east-1"
    Prefix          = "tenants"       # optional key prefix ("" = none)
    UseSSL          = false
    BucketLookup    = "path"
    SSE             = ""              # server-side encryption: "", "S3" or "SSE-C"
    PartSize        = 16777216        # bytes; larger files are stored as multipart uploads

[MetadataBackendConfig]
    Driver           = "postgres"
    ConnectionString = "host=<rds address> port=<rds port> user=<master user> password=<pw> dbname=<db> sslmode=disable"
```

Durations and sizes use the same human-readable forms as the CLI. Settings the program
refuses (for example an inconsistent TTL pair or an out-of-range part size) make the
server and the CLI exit with a nonzero status. On first use, the program creates its own
tables in the configured database. Out of scope: anonymous uploads
(`FeatureAuthentication = "enabled"`), the `sort=size` listing option, user search,
OAuth/OIDC and other external login providers, the web UI and its static files,
end-to-end encryption, QR codes, version and build information, metrics, download
domains, upload IP whitelists, the `fakedb` subcommand and the timing of the server's
own periodic cleaning.

## AWS setup

1. Create an S3 bucket with path-style addressing, or let the program create it.
2. Create an RDS PostgreSQL instance (`create_db_instance`, `Engine="postgres"`), wait
   for `db_instance_available`, and read `Endpoint.Address`/`Endpoint.Port`. Build the
   connection string from these and from the master user and password you chose.

## S3 storage

A file is stored as one S3 object named `<Prefix>/<uploadID>.<fileID>`, or
`<uploadID>.<fileID>` when no prefix is set. The object body holds exactly the
uploaded bytes. Also in scope:

* Objects stored under the older name `<Prefix>/<fileID>` (or `<fileID>`) are still
  served and removed when the new name is absent.
* Files larger than `PartSize` are written as S3 multipart uploads; smaller ones with a
  single request. Tests read the object's ETag part-count suffix.
* `SSE = "S3"` asks S3 to encrypt each object. `SSE = "SSE-C"` encrypts each object with
  a customer key the program generates per file and records in the file's
  `files.backend_details` column (a JSON object whose `SSEKey` field holds the key).
* On a bucket with versioning enabled, removing a file must leave neither a version nor
  a delete marker of its object.

## Credentials

| Credential | How it is presented | Who holds it |
|---|---|---|
| Session | cookies `plik-session` and `plik-xsrf` set by `POST /auth/local/login` | the logged-in user |
| XSRF header | `X-XSRFToken: <value of plik-xsrf cookie>`; required on non-GET/HEAD requests authenticated by session | the logged-in user |
| API token | header `X-PlikToken: <token>` | the user who created it |
| Upload capability | header `X-UploadToken: <uploadToken>` returned when an upload is created | whoever it is shared with |
| Upload password | HTTP Basic auth on downloads of a password-protected upload | whoever it is shared with |
| Impersonation | header `X-Plik-Impersonate: <userID>` added by an administrator | administrators |

API tokens are accepted on the upload/file endpoints. The `/me...` account endpoints
use the session.

## HTTP endpoints in scope

| Method | Path | Notes |
|---|---|---|
| GET | `/health` | liveness |
| GET | `/config` | feature flags and limits |
| POST | `/auth/local/login` | JSON `{"login": "...", "password": "..."}` |
| GET | `/auth/logout` | |
| POST | `/auth/cli/init` | start a CLI login; JSON `{"hostname": ...}` (optional); returns `code`, `secret`, `expiresIn` |
| POST | `/auth/cli/approve` | a logged-in user approves: JSON `{"code": ..., "comment": ...}` |
| POST | `/auth/cli/poll` | the CLI polls: JSON `{"code": ..., "secret": ...}`; returns `status` and, once approved, `token` |
| GET | `/me` | current user (JSON) |
| PATCH | `/me` | update own profile (`name`, `email`, ...) |
| DELETE | `/me` | delete own account |
| GET | `/me/token` | caller's API tokens (paginated) |
| POST | `/me/token` | JSON `{"comment": "..."}` (body optional); returns `{"token": ..., "comment": ..., "createdAt": ...}` |
| DELETE | `/me/token/{token}` | revoke a token |
| GET | `/me/uploads` | caller's uploads (paginated; optional `token=<api token>` filter) |
| DELETE | `/me/uploads` | remove the caller's uploads |
| GET | `/me/stats` | `{"uploads": n, "files": n, "totalSize": bytes}` for the caller |
| POST | `/upload` | create an upload. JSON options include `removable`, `oneShot`, `stream`, `ttl`, `extend_ttl`, `comments`, `login`/`password` (with `protectedByPassword`), and `files` (pre-declared `{"fileName": ..., "fileSize": ...}` entries). Returns the upload JSON with `id` and `uploadToken` |
| GET | `/upload/{uploadID}` | upload metadata. `admin` reports whether the caller may manage it |
| DELETE | `/upload/{uploadID}` | remove an upload |
| POST | `/file/{uploadID}` | add a file: `multipart/form-data`, part name `file`, with a filename. Returns the file JSON (`id`, `fileName`, `fileSize`, `fileMd5`, `fileType`, `status`) |
| POST | `/file/{uploadID}/{fileID}/{filename}` | upload the bytes of a pre-declared file |
| DELETE | `/file/{uploadID}/{fileID}/{filename}` | remove one file |
| GET/HEAD | `/file/{uploadID}/{fileID}/{filename}` | download. This is the public sharing link. `?dl=1` asks for an attachment; byte ranges are honoured |
| GET/HEAD/POST/DELETE | `/stream/{uploadID}/{fileID}/{filename}` | the same operations for files of a `stream` upload |
| GET/HEAD | `/archive/{uploadID}/{name}.zip` | the upload's files as one zip archive; `?dl=1` as above |
| POST | `/` | quick upload: creates an upload and adds the multipart `file` in one request |
| GET | `/users` | administrators: all users (paginated; `admin=true/false` filter) |
| GET | `/stats` | administrators: server-wide statistics |
| GET | `/uploads` | administrators: all uploads (paginated; `user=` and `token=` filters) |
| POST | `/user` | administrators: create a local user (user JSON with `login`, `password`, `provider`, limits) |
| GET/POST/DELETE | `/user/{userID}` | read, update (user JSON) or delete a user; administrators for any user |

Paginated responses have the shape `{"before": cursor|null, "after": cursor|null,
"total": n, "results": [...]}` (`total` appears on upload and user listings). They accept
`limit`, `order` (`asc`/`desc`), `before` and `after` as query parameters. Error responses
are plain text with a 4xx/5xx status.

## Upload options and limits

* Feature flags decide whether an option is refused (`disabled`), honoured on request
  (`enabled`) or imposed (`forced`): one-shot, removable, streaming, password
  protection, comments, choosing a TTL, TTL extension and account deletion. Refusals are
  4xx plain-text replies; the exact text and which state is left behind come from the
  reference.
* `MaxFileSizeStr` and the per-user `--max-file-size` limit single files, both for
  declared sizes and for the bytes actually sent. `MaxFilePerUpload` limits the files of
  one upload, both declared and added.
* A *one-shot* upload's files can be downloaded once; a *stream* upload's file bytes go
  straight from a waiting uploader to one downloader and are never written to S3.
* Archives contain the upload's uploaded files; whether entries are compressed follows
  `EnableArchiveCompression`.

## Upload lifetime and cleaning

* Every upload has a TTL in seconds: the requested `ttl`, else `DefaultTTLStr`. `-1`
  means no expiry. `MaxTTLStr` (or a user's `--max-ttl`) bounds it. `extend_ttl` asks for
  the expiry to move forward when files are downloaded.
* Once an upload's TTL has passed, the API treats it as expired.
* Deleting an upload or a file (API or CLI) and expiry do not necessarily remove
  everything at once. `clean` (also run by the server periodically) soft-deletes
  expired uploads, deletes the S3 objects of removed files and purges removed uploads
  and their files from the database. Tests observe rows and objects before and after
  `clean`, never the server's own timer.

## Tenancy model (admitted scope)

* Two or more local users are served at once, each with their own sessions and API
  tokens. An upload is recorded under the user who created it and, when an API token
  was used, under that token.
* A caller's listings, statistics and token management cover only the caller's own
  rows.
* Managing an upload (adding or removing files, deleting it) is an owner capability.
  An upload created with `removable=false` cannot be deleted by other users. The
  upload's own `X-UploadToken` capability and administrator accounts are deliberate
  exceptions. Which owner or administrator credential (session or which token)
  qualifies is part of the behaviour to discover.
* Download links are deliberately public unless the upload is password-protected.
  Knowing another user's link is not a tenancy violation.
* Per-user size limits count only the bytes of that user's own uploaded files. Limits
  are checked when an upload is created and when a file is added; the exact rule and
  what a refused file leaves behind are part of the behaviour to discover. Concurrent
  quota races are out of scope.
* Administrators can list and manage all users and uploads, and can act as another user
  with `X-Plik-Impersonate`. Users cannot raise their own limits or rights.

## Observation

Tests provision users with the CLI, start `program --config <file>` with the admitted
configuration above (one fresh database, bucket and port per test, usually
`Prefix = "tenants"`) and drive the HTTP API on `127.0.0.1`. They read the Postgres
tables `users` (`id`, `login`, `provider`, `name`, `email`, `is_admin`,
`max_file_size`, `max_user_size`, `max_ttl`), `tokens` (`token`, `user_id`, `comment`),
`uploads` (`id`, `"user"`, `token`, `upload_token`, `removable`, `one_shot`,
`comments`, `ttl`, `created_at`, `expire_at`, `deleted_at`) and `files` (`id`,
`upload_id`, `name`, `size`, `status`, `md5`, `backend_details`). In S3 they read object
keys, bodies, `ServerSideEncryption`, the ETag part-count suffix, `Content-Type`,
`ContentLength`, object versions and delete markers. CLI checks use exit status (zero on
success, nonzero on refusal), the rows above, the token printed by `token create`, the
user ids printed by `user list`, the JSON printed by `user show` and `file show`, and the
lines printed by `file list`. Metadata moved by `export`/`import` is compared through the
rows above.

Comparison rules:

* Status codes are exact. Plain-text bodies (`ok`, error messages, counts) are compared
  as text with trailing whitespace ignored.
* Headers examined: the session cookies, `Content-Type` of downloads and archives,
  `Content-Length` of `HEAD` and range downloads, `Content-Disposition`, `Cache-Control`
  and `WWW-Authenticate` on password challenges.
* Archives are compared by entry names, extracted bytes and compression method, not by
  archive bytes.
* JSON is compared by value for the fields used: upload `id`, `uploadToken`, `admin`,
  `removable`, `oneShot`, `stream`, `protectedByPassword`, `comments`, `ttl`,
  `extend_ttl`, `files`, `user`, `token`; file `id`, `fileName`, `status`, `fileMd5`,
  `fileType`, `fileSize`; token `token`, `comment`, `createdAt`; user `id`, `login`,
  `provider`, `name`, `email`, `admin`, `maxFileSize`, `maxUserSize`, `maxTTL`;
  `/me/stats` and `/stats` keys; CLI login `code`, `secret`, `expiresIn`, `status`,
  `token`; `/config` `feature_*` flags, `authentication`, `noAnonymousUploads`,
  `maxUserSize`, `maxFileSize`, `maxFilePerUpload`, `defaultTTL` and `maxTTL`. Other
  fields are not examined.
* Booleans must be JSON booleans. Numbers compare by value (`1`, `1.0`, `1e0` alike).
  An absent, `null` or empty string/list value is treated as empty, both in JSON (for
  example a withheld `uploadToken`, an unset `comment`) and for an empty `uploads.token`.
* Generated identifiers, tokens, keys, cursors and timestamps are opaque, but must be
  unique where the program makes them unique. Recorded expiry times are compared
  against creation time plus TTL to within a second.
* Upload listings are ordered by creation time; `order` selects the direction and the
  default direction is part of the behaviour. Other collections are compared without
  regard to order.
* Timing: expiry tests use TTLs of a second and wait several seconds; streaming tests
  retry the download until the uploader is attached.

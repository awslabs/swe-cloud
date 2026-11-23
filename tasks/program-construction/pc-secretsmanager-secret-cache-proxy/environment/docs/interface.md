# secret-cache-proxy — interface

`secret-cache-proxy` is a long-running local HTTP service. It reads secrets from AWS
Secrets Manager on behalf of local clients, keeps them in an in-memory cache, and
serves them on a loopback port. It can only read secrets; it never modifies them.

## Invocation

```
program sm start [--config <FILE>]     # -c <FILE> is the short form
program --help | --version
program sm start --help
```

`sm start` runs in the foreground until it receives SIGINT or SIGTERM. When it is ready,
it prints a line like this to stdout:

```
Secrets Manager Provider/<version> listening on http://127.0.0.1:<port>
```

The server listens only on `127.0.0.1`. If you pass no `--config`, built-in defaults are
used. A config file that is invalid (unknown keys, out-of-range values, malformed TOML)
makes startup fail with a non-zero exit status and a diagnostic on stderr.

The program also contains a certificate-export capability (`acm ...`) and a
`setup-config-based-permissions` command. Both are outside this task's scope.

## AWS endpoint and credentials

The program uses the standard AWS SDK configuration chain:

- `AWS_ENDPOINT_URL`, or the service-specific `AWS_ENDPOINT_URL_SECRETS_MANAGER` and
  `AWS_ENDPOINT_URL_STS`.
- `AWS_REGION` / `AWS_DEFAULT_REGION`, or the `region` config key.
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` and an optional `AWS_SESSION_TOKEN`.

In this environment all AWS APIs are answered by an emulator at `http://engines:4566`.
The endpoint, region `us-east-1` and dummy credentials are already exported.

Unless `validate_credentials = false`, startup first calls STS `GetCallerIdentity` to
check the credentials. During operation the program calls Secrets Manager
`GetSecretValue` and `DescribeSecret`. Pre-fetch uses `BatchGetSecretValue`.

## Configuration file (TOML)

The recommended layout is nested:

```toml
[logging]
log_level = "info"        # debug | info | warn | error | none (case-insensitive)
log_to_file = true        # false: log to stdout/stderr instead of ./logs/

[capabilities.secrets_manager]
enabled = true
http_port = 2773          # 1024..65535
region = "us-east-1"      # optional
path_prefix = "/v1/"
max_conn = 800            # 1..1000
max_roles = 20
ignore_transient_errors = true
validate_credentials = true

[capabilities.secrets_manager.cache]
ttl_seconds = 300         # 0..3600
cache_size = 1000         # 1..1000

[capabilities.secrets_manager.security]
ssrf_headers = ["X-Aws-Parameters-Secrets-Token", "X-Vault-Token"]
ssrf_env_variables = ["AWS_TOKEN", "AWS_SESSION_TOKEN", "AWS_CONTAINER_AUTHORIZATION_TOKEN"]

[capabilities.secrets_manager.prefetch]
cache_buffer_ratio = 0.8  # 0.1..1.0
max_jitter_seconds = 0    # 0..10

[[capabilities.secrets_manager.prefetch.secrets]]
secret_id = "my-secret-name-or-arn"
```

All keys are optional; the values shown are the defaults. Pre-fetch entries may also be
written inline (`secrets = [{ secret_id = "..." }]`). Tag-based pre-fetch is written as
`[[capabilities.secrets_manager.prefetch.filter_tags]]` with `key = "<TagKey>"`.

A legacy flat layout is also accepted. In it, the Secrets Manager keys sit at the top
level (`http_port`, `ttl_seconds`, `cache_size`, `ssrf_headers`, `ssrf_env_variables`,
`path_prefix`, `max_conn`, `max_roles`, `region`, `ignore_transient_errors`,
`validate_credentials`, `log_level`, `log_to_file`, `[prefetch]`). In the flat layout, numeric
values (`http_port`, `ttl_seconds`, `cache_size`, `max_conn`, `max_roles`) may also be written
as quoted strings such as `ttl_seconds = "300"`; the nested layout takes plain integers. How the two layouts
behave when mixed is something to observe.

With `log_to_file = true` (the default), logs go to `logs/secrets_manager_provider.log`
under the current working directory. Start the program in a writable directory, or set
`log_to_file = false`.

## SSRF token

At startup the program reads a token from the first environment variable in
`ssrf_env_variables` that is set. A value of the form `file://<path>` is a reference: the
token is the contents of that file. If none of the variables is set, startup fails.

Every request other than `/ping` must carry the token in one of the `ssrf_headers`.

## HTTP endpoints (GET only)

| Request | Meaning |
|---|---|
| `GET /ping` | Health check; responds `healthy`. Needs no token. |
| `GET /secretsmanager/get?secretId=<id>[&versionId=<id>][&versionStage=<label>][&refreshNow=<bool>]` | Return a secret. `secretId` is a name or ARN. |
| `GET <path_prefix><id>[?versionId=..][&versionStage=..][&refreshNow=..]` | Path form of the same lookup (default prefix `/v1/`). |

`refreshNow` accepts `true`/`false` (and numeric forms). The `roleArn` parameter, which
uses STS role chaining, is out of scope.

A successful lookup returns status 200 and a JSON object shaped like a Secrets Manager
`GetSecretValue` response. Its fields include `ARN`, `Name`, `VersionId`,
`SecretString` or `SecretBinary`, `VersionStages` and `CreatedDate`.

Failures return a non-200 status. Errors from Secrets Manager are passed through as a
JSON body of the form `{"__type": "<ErrorCode>", "message": "..."}`, with an HTTP status
derived from the service response. Request and authorization problems (bad token,
method, parameters, path or forwarding headers) produce short plain-text bodies with
4xx statuses.

## Cache behavior to observe

Secrets are cached in memory for `ttl_seconds`. A secret that changes in Secrets Manager
may therefore still be served with its old value until the entry expires or the client
asks for `refreshNow=true`. Several things are not specified here and should be
established by running the program:

- which request shapes share cache entries;
- what an expired entry costs (which AWS calls it causes) and what it returns;
- how version-stage moves (`AWSCURRENT`, `AWSPENDING`, `AWSPREVIOUS`) and new labels
  interact with cached entries;
- which entry is dropped when the cache is full;
- how pre-fetched entries are keyed, and how many secrets are pre-fetched;
- what happens when Secrets Manager fails or cannot be reached, with and without
  `ignore_transient_errors`;
- exact status codes and bodies, including how request paths and query values are
  decoded into a secret id.

Restarting the program empties the cache. `max_conn` limits concurrently open client
connections (an idle keep-alive connection counts); `/ping` is treated more leniently.

## How behavior is observed and compared

- **Startup.** Readiness is detected by polling `GET /ping`; the ready line must appear on
  stdout (log lines may also appear there). A rejected configuration or a failed startup
  check must exit non-zero without listening. Its stderr diagnostic should name the
  offending configuration key, environment variable, file or port; wording is not
  compared. `enabled = false` exits with status 0 after a notice on stderr saying that
  Secrets Manager is not enabled. SIGTERM and SIGINT stop the program with status 0.
- **Successful lookups** (status 200) are decoded as a JSON object. It must contain exactly
  `ARN`, `Name`, `VersionId`, one of `SecretString` / `SecretBinary` (base64 of the bytes),
  `VersionStages` and `CreatedDate`, omitting any field that Secrets Manager's own answer
  omits (no `null` placeholders). Strings are compared exactly; `VersionStages` is compared
  as a list of strings ignoring order; `CreatedDate` is a JSON string of epoch seconds with
  an optional decimal fraction, compared by value to the millisecond. Key order and
  whitespace are not compared. A cached answer must be the same document that was served
  when the entry was stored.
- **Errors.** Secrets Manager errors are JSON objects compared exactly, with types kept
  (strings stay strings). Plain-text bodies are compared after trimming surrounding
  whitespace. Status codes are always compared.
- **AWS traffic.** Tests may set `AWS_ENDPOINT_URL` to a local forwarding endpoint in front
  of the emulator. It records the Secrets Manager and STS operations the program calls and
  can answer some of them with injected failures (HTTP 4xx/5xx with JSON error bodies, or
  refused connections). For each HTTP request whose AWS calls succeed, the sequence of
  operation names it causes is compared (for example, none for a cache hit). Retrying a
  failing call is allowed and the number of attempts is not compared. Pre-fetch calls are
  compared together with their `SecretIdList`, `Filters` and `MaxResults`, and whether they
  continue with a `NextToken`.

## Observation tips

- Choose a free `http_port` for each instance; several instances can run side by side.
- Create, update and relabel secrets with boto3 (`create_secret`, `put_secret_value`,
  `update_secret_version_stage`). Then compare the program's HTTP responses with
  `get_secret_value` / `describe_secret` made directly against the emulator.
- Short `ttl_seconds` values (for example 2) make expiry observable.
- A small forwarding HTTP server between the program and the emulator (honoring
  `AWS_ENDPOINT_URL`) shows which operations a request causes; reading the
  `X-Amz-Target` header is enough for Secrets Manager.

# sealed-env-manager: interface

sealed-env-manager is a command-line secret manager. A project config file,
`fnox.toml`, declares **providers** (where a value lives or which key encrypts
it), **secrets** (environment-variable names and how each one resolves),
optional **profiles**, and optional **lease backends** that mint short-lived
credentials. The program resolves secrets through AWS and prints them, puts them
in a child process environment, or writes new values back to AWS and the config.

This guide covers the admitted scope: the AWS providers `aws-kms`, `aws-ps` and
`aws-sm`, the `aws-sts` lease backend, and the commands listed below. Other
provider types, the daemon, proxy/sandbox, TUI, MCP server and shell activation
hooks are out of scope.

## Invocation

```
program [GLOBAL FLAGS] <COMMAND> [ARGS]
```

The executable is one native program. Put global flags before the command,
and in `exec` put them before `--`. Everything after `--` belongs to the child.

Global flags:

| Flag | Meaning |
| ---- | ------- |
| `-c, --config <FILE>` | Config file. Default `fnox.toml`. The bare names `fnox.toml`, `.fnox.toml`, `fnox.local.toml` and `.fnox.local.toml` are searched in the current directory and its parents. Any other path, such as `./other.toml`, loads only that file (plus the global config). |
| `-P, --profile <NAME>` | Active profile. Repeat or comma-separate to layer several. Defaults to `FNOX_PROFILE`, then `default`. |
| `--write-profile <NAME>` | Profile that write commands change when several profiles are active. |
| `--if-missing <error\|warn\|ignore>` | Override `if_missing` for every secret. |
| `--no-defaults` | Use only the selected profile's secrets, without the top-level `[secrets]`. |
| `--non-interactive` | Never prompt (also `FNOX_NON_INTERACTIVE`). |
| `--no-color`, `-v/--verbose` | Output styling and debug logging on stderr. |
| `-V, --version`, `-h, --help` | Version and help. Every command also accepts `--help`. |

### Admitted commands

| Command | Purpose |
| ------- | ------- |
| `set [FLAGS] <KEY> [VALUE]` | Store a value with a provider and record `KEY` in the config file in the current directory. Flags: `-p/--provider`, `-k/--key-name`, `-d/--description`, `--default`, `--if-missing`, `--from-file`, `--base64-encode`, `-n/--dry-run`, `-g/--global`, `--generate` with `--generate-type`/`--length`/`--show`/`--force`. With `VALUE` omitted, piped stdin is read. |
| `get [--base64-decode] <KEY>` | Print one resolved value followed by a newline. |
| `list [-f/--full] [-s/--sources] [-V/--values]` | Table of the declared secrets. |
| `export [FLAGS] [KEY]...` | Print resolved secrets: `-f/--format env\|shell\|json\|yaml\|toml` (default `env`), `-o/--output FILE`, `--all`, `--header`, `-n/--dry-run`. |
| `env --json [--keys K,...] [--for exec\|shell] [--describe]` | One line of JSON describing the environment `exec` would build. |
| `exec [FLAGS] -- COMMAND [ARGS]...` | Run a command with the resolved secrets (and lease credentials) in its environment and exit with its status. Flags: `--only`, `--except`, `--redact`, `--no-redact`, `--replace`. |
| `remove [-g] [-n/--dry-run] <KEY>` | Remove a secret from the config file. |
| `import [FLAGS] [env\|json\|yaml\|toml]` | Encrypt key/value pairs from `-i/--input FILE` (or stdin) with the encryption provider `-p/--provider` into the config. `-f/--force` skips the confirmation prompt (required for non-interactive use), `-n/--dry-run`, `--filter REGEX`, `--prefix STR`, `-g`. |
| `reencrypt [FLAGS] [KEY]...` | Decrypt and re-encrypt stored ciphertext with the provider's current settings. `-p/--provider`, `--filter`, `-f/--force` (no prompt), `-n/--dry-run`. |
| `check [-a/--all]` | Verify that secrets resolve without printing them; problems go to stderr. |
| `lease create [FLAGS] [BACKEND]` | Mint credentials from a lease backend and record them in the lease ledger. `-d/--duration` (`15m`, `1h`, `2h30m`, ...), `-f/--format shell\|json\|env`, `-l/--label`, `-a/--all`. |
| `lease list [--active\|--expired] [--json]` | Show ledger entries. |
| `lease revoke [-a/--all] [TARGET]` | Revoke by lease ID, unique ID prefix, or backend name. |

Read `program <command> --help` for exact flag spellings and defaults.
Interactive prompts need a terminal. In tests, pass values as arguments or
on stdin, and pass `--force` where a command asks for confirmation.

## AWS venue

The venue provides an AWS-compatible endpoint. `AWS_ENDPOINT_URL`
(`http://engines:4566`), `AWS_REGION`/`AWS_DEFAULT_REGION` (`us-east-1`) and
dummy `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` are set in the environment.
The program uses the standard AWS SDK configuration chain, so it honours these
variables. Every provider and lease backend also accepts an `endpoint` field in
the config, which overrides the endpoint for that entry. IAM policies are not
enforced, but `AssumeRole` only succeeds for a role that exists in the venue.

Services used: KMS (`Encrypt`, `Decrypt`), SSM Parameter Store (`GetParameter`,
`GetParameters`, `PutParameter`), Secrets Manager (`GetSecretValue`,
`BatchGetSecretValue`, `PutSecretValue`) and STS (`AssumeRole`).

## Configuration file

`fnox.toml` is TOML. Its top-level tables:

```toml
root = true                     # optional: do not merge configs from parent directories
default_provider = "kms"        # optional
if_missing = "warn"             # optional: error | warn | ignore

[providers.kms]
type = "aws-kms"
key_id = "alias/app"            # key ID, key ARN or alias
region = "us-east-1"
endpoint = "http://engines:4566"   # optional

[providers.ps]
type = "aws-ps"
region = "us-east-1"
prefix = "/app/dev/"            # optional, prepended verbatim to names

[providers.sm]
type = "aws-sm"
region = "us-east-1"
prefix = "app/"                 # optional, prepended verbatim to names

[secrets]
API_TOKEN   = { provider = "kms", value = "<base64 KMS ciphertext>" }
DB_URL      = { provider = "ps", value = "DB_URL" }
DB_PASSWORD = { provider = "sm", value = "db-credentials", json_path = "password" }
LOG_LEVEL   = { default = "info" }

[profiles.staging.providers.ps]
type = "aws-ps"
region = "us-east-1"
prefix = "/app/staging/"

[profiles.staging.secrets]
LOG_LEVEL = { default = "debug" }

[leases.aws]
type = "aws-sts"
region = "us-east-1"
role_arn = "arn:aws:iam::000000000000:role/dev-role"
duration = "15m"                # optional
endpoint = "http://engines:4566"   # optional
```

Provider fields:

| Type | Required | Optional |
| ---- | -------- | -------- |
| `aws-kms` | `key_id`, `region` | `profile`, `role_arn`, `endpoint`, `auth_command` |
| `aws-ps` | `region` | `prefix`, `profile`, `role_arn`, `endpoint`, `auth_command` |
| `aws-sm` | `region` | `prefix`, `profile`, `role_arn`, `endpoint`, `auth_command` |

`role_arn` has the form `arn:aws:iam::<12-digit account>:role/<name>`. When it
is set, the provider assumes that role before its own calls. A provider field
may be written as `{ secret = "NAME" }` to take its value from another secret.

Secret fields, all optional: `provider`, `value`, `default`, `if_missing`
(`error`/`warn`/`ignore`), `description`, `env` (`true`, `"exec"`, `false`),
`json_path`, `line`, `as_file`. Secret names must match
`^[A-Za-z_][A-Za-z0-9_]*$`.

In `json_path`, a backslash escapes a literal dot in a key: `a\.b` selects the key `"a.b"`.

- For `aws-kms`, `value` is the base64 (standard alphabet) encoding of the KMS
  ciphertext blob, stored inline in the config.
- For `aws-ps` and `aws-sm`, `value` is the parameter/secret name. The
  provider `prefix` is prepended to it to form the remote name.

Profiles: the top level is the `default` profile. `[profiles.<name>]` may
contain `providers`, `secrets`, `leases`, `default_provider` and
`inherits = ["other", ...]`. A profile can also live in `fnox.<name>.toml` next to `fnox.toml`.
Write commands (`set`, `remove`, `import`) target the active profile.

Lease backend `aws-sts` fields: `region`, `role_arn` (required); `profile`,
`endpoint`, `duration` (optional).

## Local state

- Global config: `$FNOX_CONFIG_DIR/config.toml`, default
  `$HOME/.config/fnox/config.toml`. It is merged under project configs.
- Lease ledger: kept per project under `$FNOX_STATE_DIR`, default
  `$XDG_STATE_HOME/fnox` or `$HOME/.local/state/fnox`.

Use an isolated `HOME` (or set these variables) per scenario so global config
and ledger state do not leak between runs.

## Observation rules

- Results are observed through exit status, stdout, the environment and
  stdout/stderr of `exec` children, the config files (project, profile and
  global), AWS state read back through the AWS API, and `lease list --json`.
- Config files are compared as parsed TOML (tables, keys and values), not byte
  for byte. Comments and unrelated entries must survive edits. A command that
  refuses or fails must leave the config file unchanged.
- Inline KMS values are checked by decrypting them with KMS under the
  provider's key. Ciphertext bytes differ on every encryption and are not
  compared.
- `get` output is compared exactly (the value plus one newline).
- `export --format json|toml` and `env --json` output is compared as parsed
  data. Numbers are compared by value (`6` and `6.0` are equal; booleans are not
  numbers). `export --format shell` output is evaluated by bash. The `env` and
  `yaml` formats are graded only for values that contain no quotes, backslashes
  or newlines; how such characters are escaped there is not graded. The `yaml`
  check reads the top-level `secrets:` block (first line) as indented
  `KEY: value` lines, with optional quotes around the value.
- In `env --json`, only the `set`, `files`, `missing`, `remove` (for declared
  secrets), `leases` and `keys` members are graded. When it fails, `env --json`
  still prints a JSON document on stdout; only the presence of its `error`
  member (and the non-zero exit status) is graded.
- `list` is graded only for the presence of secret names and descriptions (and
  of values with `--values`), not for its table layout.
- Lease credentials are checked for presence and by using them against the AWS
  API. Lease IDs are opaque strings; timestamps are RFC 3339 and are checked
  against the requested duration, not as fixed values. Where the ledger file
  lives is checked only as "somewhere under the state directory".
- Files under the state directory are read as raw bytes: a lease's ID must
  appear there, and when `default_provider` names an encryption provider, the
  lease's secret access key and session token must not appear in plain text.
- Diagnostic wording on stderr is not graded. A non-zero exit status is graded
  where an operation must fail.

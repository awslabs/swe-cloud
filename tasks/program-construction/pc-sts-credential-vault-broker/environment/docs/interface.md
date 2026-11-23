# credential-vault-broker — public interface

`credential-vault-broker` keeps long-term AWS access keys in an encrypted local
store and hands out temporary STS credentials for profiles defined in an AWS
config file. It is a single executable, invoked as `/reference/program` (the
reference) and `/app/program` (yours):

```
program [<global flags>] <command> [<flags>] [<args> ...]
```

Use `program --help`, `program <command> --help` and `program --help-long` for the
exact flag syntax. The usage text names the tool by its upstream command name; that
text is not graded, but the flags, messages and exit statuses below are.

## Environment and the AWS venue

All AWS calls go to an AWS-compatible endpoint (in this venue
`http://engines:4566`, region `us-east-1`, any non-empty dummy credentials).

* The endpoint can come from the `AWS_ENDPOINT_URL` environment variable or from
  an `endpoint_url` key in the config file. Which source wins, and what `exec`
  passes on to its child, is behaviour to observe.
* Only STS (`GetSessionToken`, `AssumeRole`, `GetCallerIdentity`) and IAM
  (`GetUser`, `CreateAccessKey`, `DeleteAccessKey`) are used by the admitted scope.
  An STS `AssumeRole` target role must exist in IAM.
* The AWS config file is `$AWS_CONFIG_FILE`, else `~/.aws/config` (`$HOME` must be
  set and writable).
* An environment variable set to the empty string is treated as unset. The
  graders always run with the venue's `AWS_ENDPOINT_URL`, `AWS_REGION`,
  `AWS_DEFAULT_REGION`, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` present,
  and blank some of them when a test needs them absent.

### Credential store (admitted backend: `file`)

```
AWS_VAULT_BACKEND=file              # or the global flag --backend=file
AWS_VAULT_FILE_PASSPHRASE=<secret>  # non-interactive passphrase
AWS_VAULT_FILE_DIR=<dir>            # or --file-dir; default ~/.awsvault/keys/
```

Each stored item (a long-term credential or a cached session) is one file in the
store directory, created if missing. The reference encrypts each file as a compact
JWE (RFC 7516) with a key derived from the passphrase (PBES2); the Python
`jwcrypto` and `cryptography` packages are installed if you want to inspect it.
File names and the encryption format are not graded. What is graded: secrets never
appear in plaintext in any file, a different passphrase cannot read them, and the
store is observed through `list`, `export` and `exec`.

## Config file

Standard AWS INI syntax: `[profile NAME]` sections and `[default]`. Profile names
are case sensitive. Keys used by the admitted scope:

| key | meaning |
| --- | --- |
| `region` | region for the exported/child environment |
| `endpoint_url` | AWS endpoint base URL |
| `source_profile` | profile whose credentials are used to obtain this profile's credentials |
| `role_arn` | role to assume with `AssumeRole` |
| `role_session_name` | session name for `AssumeRole` |
| `duration_seconds` | requested `AssumeRole` duration |
| `external_id` | external id passed to `AssumeRole` (accepted; this venue does not check it, so it is not graded) |
| `include_profile` | inherit keys from another profile |
| `aws_account_id` | account id reported for profiles whose account is not otherwise known |

A profile may also exist only as stored credentials, without a config section.

Durations may also be set by `--duration`/`-d` (Go duration syntax such as `15m`,
`2h`) or `AWS_VAULT_DURATION`, and by the `AWS_SESSION_TOKEN_TTL`,
`AWS_ASSUME_ROLE_TTL` and `AWS_MIN_TTL` (how close to expiry a cached session may
get before it is replaced; default 5 minutes) environment variables. The region may also come from the `AWS_REGION` and
`AWS_DEFAULT_REGION` environment variables and the `--region` flag. Precedence
among all of these sources, which profile's settings apply along a
`source_profile` chain, and when an STS call is made or a cached session reused
are central behaviour to reconstruct from the reference.

## Commands in scope

| command | purpose |
| --- | --- |
| `add [--env] [--[no-]add-config] <profile>` | store an access key pair for a profile; `--env` reads `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` from the environment |
| `list` / `ls [--profiles\|--sessions\|--credentials]` | table of config profiles, stored credentials and cached sessions, or one of those lists |
| `remove` / `rm [-f] <profile>` | delete a profile's stored credentials; without `-f` it asks for confirmation on the controlling terminal (not standard input) |
| `exec [flags] <profile> -- <cmd> [<args>...]` | run a command with temporary credentials in its environment |
| `export [flags] [--format=env\|export-env\|json\|ini] <profile>` | print the credentials instead of running a command (default format `env`) |
| `rotate [-n] <profile>` | replace the stored access key with a newly created IAM access key |
| `clear [<profile>]` | delete cached sessions (all, or one profile's) |

Common `exec`/`export` flags: `-d/--duration`, `-n/--no-session` (no
`GetSessionToken` session), `--region`.

`exec` and `export` refuse to run inside an environment already prepared by
`exec` (the `AWS_VAULT` variable is set). Interactive input (terminal prompts, the
default `$SHELL` of `exec`, profile pickers) is out of scope: tests always pass a
profile and a command, and supply secrets through the environment.

`rotate` talks to the live IAM service. Against this venue some forms of `rotate`
fail at IAM; which ones is observable with the reference. A failed `rotate` must
leave the IAM keys, the stored key and the cached sessions unchanged.

## Observing results and comparison rules

* `exec`: the child's **complete** environment (which inherited variables are
  kept, replaced or removed, and which are added), its standard streams and its
  exit status, which `exec` returns as its own. The command is started directly
  with the given arguments; nothing beyond the reference's changes may appear in
  the child's environment (for example a wrapper shell's `PWD` or `SHLVL`).
  This includes removing the legacy `AWS_CREDENTIAL_FILE` variable from the
  inherited environment.
* `export --format=json` prints one credential-process style JSON object
  (`Version`, `AccessKeyId`, `SecretAccessKey`, optional `SessionToken`,
  `Expiration`, `AccountId`). Members are compared by name and value; member
  order and whitespace are not graded. `Version` is compared as a number
  (`1` and `1.0` are equivalent; a boolean is not a number).
* `env`/`export-env` print `NAME=value` lines (with an `export ` prefix for
  `export-env`), each variable at most once; line order is not graded.
* `ini` prints one section named after the profile; it is parsed as INI, so key
  order and spaces around `=` are not graded.
* Expirations are UTC timestamps written exactly as `YYYY-MM-DDTHH:MM:SSZ`. They
  are compared with the expected lifetime within a few seconds. Credentials are
  checked with `sts:GetCallerIdentity`; access keys with `iam:ListAccessKeys`.
* `list --sessions` prints one `<session type>:<remaining lifetime>` line per
  cached session, the lifetime as a Go-style duration such as `59m58s` or
  `1h59m59s` (compared within a few seconds; line order is not graded).
  `list --profiles` and `list --credentials` print one name per line.
* The `list` table is a header row, a separator row and one row per profile,
  laid out in space-aligned columns. Cell values, row order and alignment are
  graded; trailing padding is not.
* Standard output of `add`, `remove`, `clear` and `rotate` (progress messages)
  is compared line by line. For failures, tests check a non-zero exit status,
  empty standard output where the reference prints nothing, unchanged state,
  and for some refusals a key phrase of the reference's diagnostic on standard
  error.

Out of scope: `login`, credential servers (`--server`, `--ecs-server`,
`--ec2-server`), SSO, web identity, `credential_process`, MFA (`mfa_serial`, MFA
prompts and tokens), `--profile-env`, `--parallel-safe`, separate session
backends, keychain and other non-file backends, `migrate-backend`.

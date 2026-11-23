# account-sweeper: interface

`account-sweeper` deletes resources from one AWS account. It reads a YAML
configuration, checks that the account may be swept, finds resources of the
selected types, applies the configured filters, and then reports what it would
remove (a dry run, the default) or removes them (`--no-dry-run`).

The program is a single executable, `program`. In this environment it runs next to
an AWS emulator that serves every AWS API at `$AWS_ENDPOINT_URL`
(`http://engines:4566`), with region `us-east-1` and test credentials.

## Invocation

```
program run --config CONFIG.yaml --default-region us-east-1 --no-prompt [options]
```

`nuke` is an alias for `run`. These are the options in scope:

| Option | Meaning |
| --- | --- |
| `--config PATH`, `-c PATH` | Configuration file. The default is `config.yaml`, and the file must exist. |
| `--default-region NAME` | Region used to set up the AWS session (env `AWS_DEFAULT_REGION`). In this environment it is always the custom endpoint region `us-east-1`. |
| `--no-dry-run` | Actually remove resources. Without it, nothing is changed. |
| `--no-prompt` (alias `--force`) | Do not ask for interactive confirmation. Wait `--prompt-delay` seconds instead, before scanning and again before removing. |
| `--prompt-delay N` (alias `--force-sleep`) | Seconds to wait when `--no-prompt` is set. The default is 10, and values below 3 are rejected. |
| `--quiet`, `-q` | Hide the lines for filtered resources. |
| `--include TYPE` (alias `--target`), `--exclude TYPE` | Repeatable command-line limits on resource types. They are combined with the configuration's `resource-types`. |
| `--run-sleep-delay DURATION` | Pause between removal/verification rounds (Go duration, e.g. `2s`). The default is `5s` (env `AWS_NUKE_RUN_SLEEP_DELAY`). |
| `--max-wait-retries N` | Limit on rounds spent waiting for resources to disappear. The default is 0, meaning no limit. |
| `--feature-flag filter-groups` | Enables filter groups (see the configuration guide). |
| `--no-alias-check` | Skips both the missing-alias check and the alias blocklisted-term check, but only for accounts listed in `bypass-alias-check-accounts`. Account membership and account blocklist checks still apply. |
| `--access-key-id`, `--secret-access-key`, `--session-token` | Credentials (default to the standard `AWS_*` environment variables). |
| `--log-disable-colors`, `--log-level LEVEL`, `--log-format standard\|json`, `--json` | Log presentation. |

`program --help`, `program run --help`, `program version` and
`program resource-types` print usage, the version and the list of resource-type
names.

### Accounts in the emulator

The emulator decides which account a request belongs to from the access key ID. A
key of exactly 12 digits is its own account (`sts get-caller-identity` returns that
number), and every other key maps to one shared default account. Each account has
its own resources and its own IAM account alias. You can manage the alias with
`iam create-account-alias`, `iam list-account-aliases` and
`iam delete-account-alias`.

## Reaching AWS: the `endpoints` block

The program only talks to the endpoints listed in the configuration. A working
block for this environment looks like this:

```yaml
endpoints:
  - region: us-east-1
    services:
      - service: sts
        url: http://engines:4566
      - service: iam
        url: http://engines:4566
      - service: sqs
        url: http://engines:4566
      - service: sns
        url: http://engines:4566
      - service: ssm
        url: http://engines:4566
      - service: secretsmanager
        url: http://engines:4566
      - service: kms
        url: http://engines:4566
      - service: cloudwatchlogs
        url: http://engines:4566
      - service: cloudwatch
        url: http://engines:4566
      - service: cloudformation
        url: http://engines:4566
      - service: scheduler
        url: http://engines:4566
      - service: ""
        url: http://engines:4566
```

* `region` must match `--default-region` and the region listed under `regions:`.
* When `sts` and `iam` are both listed, the account ID comes from STS
  `GetCallerIdentity` and the aliases come from IAM `ListAccountAliases`, both
  sent to the URL of the entry whose `service` is the empty string. That entry is
  required in that case, and it must come **last**.
* Each resource type is served by the first `service` entry whose name is a
  case-insensitive prefix of the type name. For example, `sqs` serves `SQSQueue`.
  With the order above, `cloudwatchlogs` serves `CloudWatchLogsLogGroup` and
  `cloudwatch` serves `CloudWatchAlarm`. All entries here point to the same
  emulator.
* The endpoints are plain HTTP. `tls_insecure_skip_verify` is not needed.

## Scope

These are the resource types in scope. Configurations select them through
`resource-types.includes` (or `--include`):

`SQSQueue`, `SNSTopic`, `SNSSubscription`, `SSMParameter`, `SecretsManagerSecret`,
`KMSKey`, `KMSAlias`, `CloudWatchLogsLogGroup`, `CloudWatchAlarm`,
`CloudFormationStack`, `SchedulerSchedule`.

These are also in scope:

* The account safety gate (blocklist, account listed in `accounts:`, alias
  required, alias blocklist terms).
* Filters: `exact`, `contains`, `glob`, `regex`, `dateOlderThan` and
  `dateOlderThanNow`, `invert`, property filters such as `tag:<key>` and `Name`,
  `__global__` filters, presets and filter groups.
* Dry run versus real removal, including retrying until the account is stable and
  waiting for pending states.

All other resource types, Cloud Control alternatives, IAM resource deletion and
multi-region runs (`regions: [all]`, several regions) are out of scope. Only the
single region `us-east-1` is used.

The configuration language is described in [configuration.md](configuration.md).

## Output and exit status

All log and report lines go to standard output. Each discovered resource is
reported on one line:

```
<region> - <ResourceType> - <identifier> - [<Property>: "<value>", ...] - <status>
```

This is the `standard` log format without colors (`--log-disable-colors`), which is
how the program is run when results are compared. Other presentation options are
not compared.

For example, `us-east-1 - SSMParameter - /app/db-url - [Name: "/app/db-url", tag:team: "core"] - would remove`.

* `<status>` is `would remove` for a resource that would be deleted.
  `filtered: <reason>` marks one that is kept, for example
  `filtered: filtered by config` for a configuration filter. During a real run you
  also see progress states such as `triggered remove`, `waiting for removal`,
  `removed` and `failed`.
* `<identifier>` is the resource's own string form, which differs by type (a queue
  URL, a parameter name, an ARN, a key ID, ...). The bracketed properties are the
  names that property filters can refer to. Run a dry run to see both for a
  resource.
* The reported identifiers and their would-remove / filtered category are
  significant, as reported during the scan on standard output. Each discovered
  resource gets exactly one such line, so two resources with the same identifier
  give two lines. Any `filtered: <reason>` counts as filtered, whatever the
  reason. Progress lines, colors, text before the region (such as a timestamp),
  line order, spacing around the ` - ` separators, the bracketed properties and
  other log lines are not significant.
* Exit status is 0 when the run completes. This includes a dry run, a run with
  nothing to delete and a completed removal. Exit status is nonzero when the
  configuration or safety gate refuses the account, when a filter cannot be
  evaluated, when removal ends with resources in a failed state, or when
  `--max-wait-retries` is exhausted. A refused run, or one that stops while
  scanning, changes nothing.

## Observing results

Inspect results through the AWS APIs (for example with boto3 against
`$AWS_ENDPOINT_URL`): which queues, topics, subscriptions, parameters, secrets,
log groups, alarms, stacks and schedules remain, the `KeyState` of KMS keys, and
which KMS aliases remain. A dry run must leave every resource untouched. Only
resources of the selected types are removed; for example, Scheduler schedule
groups and IAM account aliases are left in place.

# statemachine-schedule-sync — interface

`statemachine-schedule-sync` is a command-line tool that keeps AWS Step Functions state
machines and their EventBridge Scheduler schedules in sync with a declarative Jsonnet
configuration, much like a small, special-purpose Terraform. It keeps no local state file.
Each run compares the configuration with what AWS currently reports and then plans or
applies the difference.

The reference executable is `/reference/program`; the program you build is `/app/program`.
Relative paths (config file, `-o` report) resolve against the current directory.

## Invocation

```
program <COMMAND> [OPTIONS]

Commands:
  apply   apply config
  plan    plan config
  import  import state machine to specified config file
  help    Print this message or the help of the given subcommand(s)
Options:
  -h, --help     Print help
  -V, --version  Print version
```

`plan` and `apply` take the same options:

| option | meaning |
|---|---|
| `-c, --config <path>` | Jsonnet config file. Default: `fubura.jsonnet` in the current directory. |
| `-V, --ext-str <key=[val]>` | Jsonnet external string variable, read with `std.extVar('key')`. Repeatable. `key` without `=` binds `null`. Each key may be given only once. |
| `-t, --target <name>` | Limit the run to the config entries whose state machine name matches. Repeatable. |
| `-o, --diff-as-json <path>` | Also write the computed diff as a JSON document to `path`. |
| `--debug` | Verbose diagnostic logging. |
| `-a, --auto-approve` | (`apply` only) Apply without asking for confirmation. |

Without `--auto-approve`, `apply` prints the plan and then asks on stdin for confirmation.
Only the exact answer `yes` proceeds. Any other answer (including end of input) cancels the
run: `apply cancelled!` on stderr, a non-zero exit, and no AWS changes. When there is nothing
to change, `apply` does not ask.

`import` options:

| option | meaning |
|---|---|
| `-c, --config <path>` | Config file to write. Default: `fubura.jsonnet`. If it already exists it is evaluated first (with `-V` variables) and its entries are kept. |
| `-V, --ext-str <key=[val]>` | As above, used when evaluating an existing config file. |
| `-f, --sfn-name <name>` | Name of the existing state machine to import (required). |
| `-s, --scheduler-name-with-group <group/name>` | Existing schedule to import with it (optional). |
| `--debug` | Verbose diagnostic logging. |

## AWS setup

The program uses the standard AWS SDK configuration chain:

- `AWS_ENDPOINT_URL`: endpoint for all services (here `http://engines:4566`).
- `AWS_REGION`: required. ARNs of state machines are derived from the region and from the
  account returned by STS `GetCallerIdentity`.
- `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`: any dummy credentials.

Services used: Step Functions, EventBridge Scheduler and STS. The program never creates
schedule groups, IAM roles, log groups or the target resources a definition refers to. A
non-default schedule group has to exist before a schedule is created in it. Role and other
ARNs are passed through and are not validated by the emulator.

## Configuration

The config file is Jsonnet. It must evaluate to an **array** of entries:

```jsonnet
[{
  state: { ... },          // required
  schedule: { ... },       // optional (may be omitted or null)
  deleteAll: false,        // optional, default false
  deleteSchedule: false,   // optional, default false
}]
```

The field names follow the AWS `CreateStateMachine` and `CreateSchedule` request
parameters, written in camelCase. Unknown fields are ignored.

### `state`

| field | notes |
|---|---|
| `name` | State machine name; identifies the entry. |
| `definition` | The Amazon States Language definition as a JSON **object** (not a string). |
| `roleArn` | Execution role ARN. |
| `type` | `STANDARD` or `EXPRESS`. |
| `loggingConfiguration` | Optional: `{ level: ALL\|ERROR\|INFO\|OFF, includeExecutionData: bool, destinations: [ { cloudWatchLogsLogGroup: { logGroupArn } } ] }`. At most one destination. Graded inputs use `ALL`, `ERROR` and `OFF`. |
| `tracingConfiguration` | Optional: `{ enabled: bool }`. |
| `tags` | List of `{ key, value }`. Required (may be empty). |

### `schedule`

| field | notes |
|---|---|
| `groupName` | Schedule group. Defaults to `default`. |
| `name` | Schedule name. |
| `description` | Optional. |
| `state` | `ENABLED` or `DISABLED`. |
| `scheduleExpression` | e.g. `rate(1 hour)`, `cron(0 12 * * ? *)`, `at(...)`. |
| `scheduleExpressionTimezone` | Optional, e.g. `UTC`. |
| `startDate`, `endDate` | Optional RFC 3339 timestamps. |
| `flexibleTimeWindow` | `{ mode: OFF\|FLEXIBLE, maximumWindowInMinutes? }`. |
| `kmsKeyArn` | Optional. |
| `target` | `{ arn, roleArn, input?, deadLetterConfig?: { arn }, retryPolicy?: { maximumEventAgeInSeconds, maximumRetryAttempts }, ... }`. `arn` is normally the ARN of the entry's state machine. |

### Deletion flags

There is no state file, so a resource is never deleted just because it disappeared from
the config. Instead, set `deleteSchedule: true` (delete the entry's schedule) or
`deleteAll: true` (delete the entry's state machine and schedule). Then run `apply` with the
entry still in the config.

### Example

```jsonnet
local job(name, account, every) = {
  state: {
    name: name,
    type: 'STANDARD',
    roleArn: 'arn:aws:iam::123456789012:role/sfn-' + name,
    definition: {
      StartAt: 'Prepare',
      States: {
        Prepare: { Type: 'Pass', Result: { job: name }, Next: 'Pause' },
        Pause: { Type: 'Wait', Seconds: 5, Next: 'Done' },
        Done: { Type: 'Succeed' },
      },
    },
    loggingConfiguration: { level: 'OFF', includeExecutionData: false, destinations: [] },
    tracingConfiguration: { enabled: false },
    tags: [{ key: 'Team', value: 'batch' }],
  },
  schedule: {
    groupName: 'default',
    name: name,
    state: 'ENABLED',
    scheduleExpression: every,
    scheduleExpressionTimezone: 'UTC',
    flexibleTimeWindow: { mode: 'OFF' },
    target: {
      arn: 'arn:aws:states:us-east-1:' + account + ':stateMachine:' + name,
      roleArn: 'arn:aws:iam::123456789012:role/scheduler',
      input: '{}',
    },
  },
};

[
  job('nightly-report', std.extVar('account'), 'cron(0 2 * * ? *)'),
  job('hourly-sync', std.extVar('account'), 'rate(1 hour)') + { deleteSchedule: true },
]
```

```
program plan  -c jobs.jsonnet -V account=000000000000 -o plan.json
program apply -c jobs.jsonnet -V account=000000000000 --auto-approve
program import -c imported.jsonnet -f nightly-report -s default/nightly-report
```

## Output and observation rules

- **Exit status**: `0` on success. A configuration, AWS or cancellation error prints a
  message on stderr and exits non-zero. Tests check "non-zero" and a non-empty stderr; the
  exact wording is only checked for `apply cancelled!`.
- **stdout of `plan` / `apply`**: for each selected entry, in config order, the entry's text
  diff and/or deletion notices (`State machine(<name>) is going to be deleted`,
  `Schedule(<name>) is going to be deleted`), or the line `no difference` when it has
  nothing to do. Then a
  final block: either the line `No diff found. Fubura will do nothing.` or the line
  `Fubura will:` followed by six lines `    <operation>: <count>`, one per operation
  (zero counts included, in any order).
- stdout also carries diagnostic log lines. Each starts with an RFC 3339 timestamp and is
  not part of the output contract; tests drop such lines and colour escapes before
  comparing. The confirmation prompt is not graded either.
- **Text diffs** compare the remote resource with the local one, each rendered as
  pretty-printed JSON (2-space indentation). A resource that does not exist remotely renders
  as `null`. The state machine section comes before the schedule section. Each hunk is
  preceded by `--- remote: <name>` and `+++ local:  <name>` (two spaces after `local:`), where
  `<name>` is the state machine or schedule name. Tests compare these headers and the
  multiset of added (`+`) and removed (`-`) lines; context lines and hunk boundaries are not
  compared. The graded edits are small enough that every minimal line diff gives the same
  added/removed lines.
- **`--diff-as-json` file**: a JSON object with exactly these keys:
  - `text_diff`: one string per entry that changes, in config order: the same text as on
    stdout for that entry, without colour escapes;
  - `diff_ops`: `{ "state_name": ..., "diff_ops": [ ... ] }` per entry that changes, in config
    order;
  - `no_change`: boolean;
  - `summary`: object with all six operation names, each mapped to the number of entries that
    perform it.

  Operation names are `create_state`, `update_state`, `create_schedule`, `update_schedule`,
  `delete_schedule` and `delete_state`, and within an entry they are listed in that order.
  Tag changes are reported as `update_state`. The file is written by both `plan` and `apply`.
- **`import`** writes the config file as pretty-printed JSON, which is valid Jsonnet: an
  array of entries in the configuration format above, with every field present (explicit
  `null`s and both deletion flags). Importing a name that the config already contains is
  refused.
- **JSON comparison**: the report file and the imported config are compared as parsed
  JSON. Booleans, strings and `null` must match exactly; numbers compare by value (`3`, `3.0`
  and `3e0` are equal). Tag lists in imported configs are compared without regard to order,
  and `startDate`/`endDate` compare as instants. Key order and whitespace do not matter.
- Graded effects are what AWS reports afterwards: state machine definition (compared as
  JSON), role, type, logging/tracing configuration and tags (as a set of key/value pairs);
  and the schedule's expression, timezone, state, group, description, start/end dates,
  flexible window and target (ARN, role, input, retry/dead-letter). Resources that a run should
  not touch are checked to be unchanged.

## Scope

In scope: `plan`, `apply` and `import` against Step Functions and EventBridge Scheduler
through the configured endpoint, Jsonnet evaluation with `--ext-str`, target filtering,
JSON diff output, tag reconciliation and the deletion flags.

Out of scope: running state machine executions or the ECS / other targets they invoke,
IAM enforcement, state machine versions and aliases, and any service other than the three
listed above.

A general-purpose Jsonnet evaluator is available to Python code as the `_jsonnet` module
(official binding, version 0.21.0): `_jsonnet.evaluate_file(path, ext_vars={...})`
returns the JSON text.

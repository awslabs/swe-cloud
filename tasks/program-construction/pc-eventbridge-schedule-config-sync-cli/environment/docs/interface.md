# schedule-rule-sync: command interface

schedule-rule-sync is a declarative command-line tool that keeps two AWS
services in line with a configuration file:

- **EventBridge Rules** (on an event bus) — triggered by a
  `scheduleExpression` *or* an `eventPattern`, each with a list of targets;
- **EventBridge Scheduler schedules** (inside schedule groups) — time-only
  triggers (`cron(...)`, `rate(...)`, one-time `at(...)`), each with a single
  target.

It can show the current AWS state as configuration (`dump`), compare it with a
desired configuration (`diff`), bring AWS in line with it (`apply`), and
remove previously managed resources that left the configuration (`-prune`).
The configuration language is described in [configuration.md](configuration.md).

## Invocation

```
program [GLOBAL FLAGS] <validate|dump|diff|apply> [name-prefix]
```

The executable is a single self-contained Linux binary; invoke it by path.
**All flags must precede the subcommand.** Arguments after the subcommand are
positional (only `dump` takes one: a name prefix); flags written after the
subcommand are not interpreted as flags. Usage text (starting with a
`usage:` line) goes to stderr: with no subcommand or an unknown subcommand the
exit code is 1, with `-h` it is 0, and an unknown flag or a malformed flag
value exits 2. The usage text and default file name refer to the tool by its
historical name; that naming is part of the interface.

| Flag | Applies to | Meaning |
| --- | --- | --- |
| `-conf FILE_OR_GLOB` | all | Configuration file, or a shell-style glob matching several files (quote it). Default `ebschedule.yaml` in the working directory. Files ending in `.jsonnet`/`.libsonnet` are Jsonnet; everything else is templated YAML. |
| `-dry-run` | `apply` | Report what would change; make no AWS mutation (AWS is still read). |
| `-auto-approve` | `apply` | Skip the interactive confirmation. The prompt only appears when stdin is a terminal; non-interactive runs never prompt. |
| `-prune` | `apply` | Also delete tracked resources that the configuration no longer declares (see *Prune scope*). |
| `-target KIND:NAME` | `diff`, `apply` | Restrict to named resources; `KIND` is `rule` or `schedule`; repeatable. Cannot be combined with `-prune`. |
| `-tag KEY=VALUE` | `dump` | Only emit Rules carrying this tag; repeatable, all must match. |
| `-version` | — | Print a version line and exit 0. |

### Subcommands

- `validate` — offline structural check of every loaded file. Makes no AWS
  calls. On success prints `OK: N config file(s) valid` to stdout and exits 0.
  Otherwise prints one problem per line to stderr, prefixed with the file path
  and a resource locator, then a final `error: ...` summary line, and exits 1.
- `dump [prefix]` — writes one YAML configuration document to stdout
  reflecting the Rules on the configured event bus and the schedules in the
  configured schedule group whose names start with `prefix` (all when
  omitted). Region, bus and group are taken from the first `-conf` file when
  one exists; a missing configuration file is not an error (defaults are used).
- `diff` — for each declared resource, prints a unified diff between the
  current AWS state (`--- KIND:NAME (current)`) and the desired state
  (`+++ KIND:NAME (desired)`), both rendered as YAML. Resources in sync print
  nothing. With several files, each file's section is preceded by
  `# <path>`.
- `apply` — creates/updates the declared resources. Stdout lists one action
  line per resource, using these markers:
  `+ KIND:NAME (create)`, `~ KIND:NAME (update)`, `= KIND:NAME (no-op)`,
  `+ schedule-group:NAME (create)`, `- rule:NAME (delete)`,
  `- schedule:NAME (delete from GROUP)`.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | success; for `diff`, no drift |
| 1 | error (message on stderr, starting with `error: `) |
| 2 | `diff` found drift; also command-line flag parse errors |

## AWS endpoint and credentials

The program uses the standard AWS SDK configuration chain: credentials and the
service endpoint come from the environment (`AWS_ENDPOINT_URL`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION` /
`AWS_DEFAULT_REGION`). In this environment those point at an AWS emulator at
`http://engines:4566`, region `us-east-1`. The `region:` key of a
configuration file selects the region for that file's EventBridge and
Scheduler calls; write `region: us-east-1` in configurations.

Services used: EventBridge (rules, targets, rule tags), EventBridge Scheduler
(schedules, schedule groups, group tags), SSM Parameter Store (only for the
`ssm` template function) and STS (`GetCallerIdentity`, used to fill
`AWS_ACCOUNT_ID` when it is unset and as a credential check before `apply`).
`validate` never contacts AWS.

## Ownership and tagging

Managed resources are identified by the tag key **`ebschedule-tracking-id`**
whose value is the configuration's `trackingId`.

- Rules carry tags individually. A Rule's desired tags are the configuration's
  top-level `tags:` merged with the rule's own `tags:` (rule wins), plus the
  tracking tag when a `trackingId` is set.
- Scheduler schedules have no tags of their own. Tags live on the schedule
  **group**: when `apply` has to create a group it tags it with the top-level
  `tags:` plus the tracking tag. The `default` group is never created or
  tagged.
- The tracking tag is never shown in `dump` or `diff` output.

## Prune scope

`-prune` requires a `trackingId` in the configuration. Only resources marked
with that tracking id are candidates: Rules on the configured bus carrying the
tag, and schedules inside schedule groups carrying the tag. Whether a
resource section participates at all follows the omitted-versus-empty rule in
[configuration.md](configuration.md).

## Out of scope

The `run` and `import-ecschedule` subcommands, the `tfstate`/`tfstatef`
template functions, and ECS, Lambda, Batch, Glue, CodeBuild, CodePipeline,
SageMaker, Redshift and API-destination targets are not part of this task.
Targets used here are SQS queues (including FIFO `sqsParameters`), SNS
topics, Kinesis streams and Step Functions state machines (and, for
schedules, an EventBridge bus). The emulator does not retain
`kinesisParameters`, so their AWS persistence and their behavior in `dump`,
`diff` and `apply` are out of scope. Offline validation of these parameters
remains in scope: when `kinesisParameters` is present, Rule targets require a
nonempty `partitionKeyPath`, and Scheduler targets require a nonempty
`partitionKey`. Interactive confirmation on a terminal is not exercised.

## Observation rules

- AWS state is observed through the AWS APIs (EventBridge `DescribeRule`,
  `ListRules`, `ListTargetsByRule`, `ListTagsForResource`; Scheduler
  `GetSchedule`, `GetScheduleGroup`, `ListSchedules`,
  `ListTagsForResource`). JSON strings stored in AWS (`Input`,
  `EventPattern`) are compared by value. Apart from tags, an empty value is
  the same as an absent one, and Scheduler's own defaults (`ActionAfterCompletion: NONE`,
  timezone `UTC`, retry policy 185 attempts / 86400 s) are the same as
  leaving the field unset; the emulator otherwise reports back what was sent.
  Tags (on rules and on schedule groups) are compared as complete key/value
  maps: a tag whose value is the empty string is a present tag.
- `apply` stdout lines, `validate`'s `OK:` line, the `# <path>` headers and
  `applied ...` summaries are compared exactly.
- `dump` output is parsed as YAML 1.2 (only `true`/`false` are booleans, dates
  stay strings) and compared structurally: mapping key order is not
  significant, sequence order is. Keys the reference leaves out must be left
  out; `null` or empty values are not the same as absence.
- `diff` output is compared line by line. `#`, `---`, `+++` and `@@` lines
  must match exactly. On every other line the change marker, the
  indentation and the YAML content must match. Scalars are compared by their
  YAML 1.2 meaning, so the quoting style is free (`"OFF"`, `'OFF'` and `OFF`
  are the same string; `"1"` and `1` are not), and numbers are compared by
  value. Booleans are never numbers. Exit codes are exact.
- `validate` problem lines are compared exactly but in any order, followed
  by the exact `error: validation failed: N problem(s)` line. Problems are
  found by the per-resource checks. Errors that stop loading are reported
  instead as a single `error: <file>: ...` line: unknown keys, template,
  Jsonnet or baseFile failures, a file with neither section, and malformed
  schedule expressions.
- Error text is graded according to where it comes from:
  - **The program's own fixed message text is graded exactly.** This covers
    every `error: ...` line it composes (for example `neither rules: nor
    schedules: present`, the baseFile messages, the schedule-expression shape
    messages, `-prune`/`-target` guards, `no config files match: ...`), and
    the messages of its own template/Jsonnet functions (for example
    `env var NAME is not set` or `ssm:/path: index 3 out of range (len=3)`).
    The `error: <file>: ` prefix and the resource locators are graded too.
  - **Text produced by embedded libraries is not graded.** This covers the
    YAML decoder, the template engine and the Jsonnet evaluator (including
    their location details around a function's message), the `cron(...)`
    field parser, the command-line flag parser, the filesystem and AWS. The
    program's fixed text in front of that library text still is graded. For
    example `error: <file>: rules[NAME]: scheduleExpression "EXPR": ` before a
    cron parser explanation, `error: <file>: baseFile PATH: ` before a
    filesystem error, or `error: ensure schedule group NAME: ` before an AWS
    error.
  - **Where only library text is involved, the offending item must still be
    named.** An unknown key's name must appear in the message, and a flag
    parse error (exit 2, usage text follows) must mention the offending flag
    or value.

# schedule-rule-sync: configuration files

A configuration is a YAML document (after template expansion) or a Jsonnet
program producing the same structure as JSON. Unknown keys are errors.
YAML is read with YAML 1.2 scalar rules: only `true`/`false` are booleans, so
unquoted `OFF`, `on` or `yes` are strings and dates are plain strings (PyYAML
follows YAML 1.1 and would read `OFF` as `false` and dates as timestamps).
A file must contain at least one of `rules:` or `schedules:`; an entirely
empty file is ignored.

## Top level

```yaml
region: us-east-1          # region for this file's AWS calls
trackingId: my-app         # ownership marker (tag value); required for -prune
tags:                      # default tags (Rules, and newly created schedule groups)
  Service: my-app
eventBusName: default      # bus for rules (default: "default")
groupName: my-app          # schedule group for schedules (default: "default")
baseFile: _base.yaml       # optional shared scaffolding, path relative to this file
rules: [...]               # EventBridge Rules
schedules: [...]           # Scheduler schedules
```

### Omitted versus empty sections

| YAML | Meaning |
| --- | --- |
| `rules:` key absent | this file does not manage Rules at all (no diff, apply or prune of Rules) |
| `rules: []` | this file manages Rules and declares none |
| `rules: [ ... ]` | this file manages exactly these Rules |

`schedules:` follows the same rule.

### `baseFile`

A base file supplies `region`, `groupName`, `eventBusName`, `trackingId` and
`tags` that the including file did not set itself; tags merge with the
including file winning on conflicts. A base file may itself name a
`baseFile`. It must not declare `rules:` or `schedules:`.

### Several files

`-conf 'dir/*.yaml'` loads every match in sorted path order. Each file is
handled independently with its own region, bus, group, tags and
`trackingId`.

## Rules

```yaml
rules:
  - name: nightly                     # [A-Za-z0-9_.-], at most 64 chars
    description: optional text
    scheduleExpression: cron(0 18 * * ? *)   # either this ...
    eventPattern:                     # ... or this (mapping or JSON string)
      source: [my.app]
    state: ENABLED                    # ENABLED | DISABLED
    roleArn: arn:aws:iam::123456789012:role/r
    tags: {Team: a}                   # merged over top-level tags
    targets:
      - id: q                         # target id, unique within the rule
        arn: arn:aws:sqs:us-east-1:123456789012:work
        roleArn: ...                  # optional
        input: {any: json}            # mapping/sequence, or a JSON string
        inputPath: $.detail           # optional
        inputTransformer:
          inputPathsMap: {b: $.detail.bucket}
          inputTemplate: '{"bucket": <b>}'
        retryPolicy: {maximumRetryAttempts: 2, maximumEventAgeInSeconds: 3600}
        deadLetterConfig: {arn: arn:aws:sqs:...:dlq}
        sqsParameters: {messageGroupId: g}           # FIFO queues
        kinesisParameters: {partitionKeyPath: $.id}  # Kinesis streams
```

## Schedules

```yaml
schedules:
  - name: weekly
    description: optional text
    groupName: other-group            # optional per-schedule group override
    scheduleExpression: cron(0 9 ? * MON *)   # cron(...) | rate(...) | at(...)
    scheduleExpressionTimezone: Asia/Tokyo    # IANA zone
    state: ENABLED                    # ENABLED | DISABLED
    startDate: 2026-01-01T00:00:00Z   # RFC 3339, optional
    endDate: 2027-01-01T00:00:00Z
    actionAfterCompletion: NONE       # NONE | DELETE (one-time schedules)
    kmsKeyArn: ...
    flexibleTimeWindow:
      mode: FLEXIBLE                  # OFF | FLEXIBLE
      maximumWindowInMinutes: 15
    target:                           # exactly one target
      arn: arn:aws:sqs:us-east-1:123456789012:work
      roleArn: arn:aws:iam::123456789012:role/scheduler   # required
      input: {any: json}
      retryPolicy: {maximumRetryAttempts: 3, maximumEventAgeInSeconds: 3600}
      deadLetterConfig: {arn: ...}
      sqsParameters: {messageGroupId: g}
      kinesisParameters: {partitionKey: literal-key}
      eventBridgeParameters: {detailType: Tick, source: my.app}
```

Schedules have no `tags:` key; see the tagging model in
[interface.md](interface.md).

## Templating (YAML files)

Before YAML parsing, each YAML file is expanded as a Go `text/template`
document with these functions:

| Call | Result |
| --- | --- |
| `{{ env "NAME" }}` | value of the environment variable, empty if unset |
| `{{ must_env "NAME" }}` | value of the variable; an error if unset or empty |
| `{{ ssm "/path" }}` | SSM Parameter Store value (decrypted) |
| `{{ ssm "/path" 1 }}` | element 1 of a comma-separated (StringList) value |

Under `validate` nothing is looked up remotely: an unset `must_env` becomes
`<env:NAME>`, `ssm` becomes `<ssm:/path>` (or `<ssm:/path[1]>`), and such
placeholders are accepted where an ARN is expected.

For `diff`, `apply` and `dump`, an unset `AWS_ACCOUNT_ID` is filled from the
caller identity before templates are expanded, so
`arn:aws:sqs:us-east-1:{{ must_env "AWS_ACCOUNT_ID" }}:work` works without
exporting it.

## Jsonnet files

`.jsonnet` / `.libsonnet` files are evaluated as Jsonnet (no text/template
step); `import` paths resolve relative to the file. Native functions are
reachable through `std.native`:

| Call | Result |
| --- | --- |
| `std.native('env')(name, default)` | variable value, or `default` when unset |
| `std.native('must_env')(name)` | variable value; error when unset |
| `std.native('ssm')(name)` | SSM parameter value |
| `std.native('ssmList')(name)` | SSM value split on commas, as an array |

Under `validate` the same placeholders as above are produced (`ssmList`
yields a one-element array of the placeholder).

## Schedule expression syntax

`cron(...)` takes the six AWS fields (minutes hours day-of-month month
day-of-week year) and is checked when the file is loaded by any subcommand;
`rate(N unit)` and `at(yyyy-mm-ddThh:mm:ss)` are checked for shape.

## Example

```yaml
region: us-east-1
trackingId: billing
groupName: billing
tags:
  Service: billing
  Stage: '{{ env "STAGE" }}'
rules:
  - name: billing-on-invoice
    eventPattern:
      source: [billing.app]
      detail-type: [Invoice Created]
    state: ENABLED
    targets:
      - id: topic
        arn: arn:aws:sns:us-east-1:{{ must_env "AWS_ACCOUNT_ID" }}:invoices
schedules:
  - name: billing-close-month
    scheduleExpression: cron(0 0 1 * ? *)
    scheduleExpressionTimezone: Europe/Paris
    state: ENABLED
    flexibleTimeWindow: {mode: "OFF"}
    target:
      arn: arn:aws:sqs:us-east-1:{{ must_env "AWS_ACCOUNT_ID" }}:month-close
      roleArn: arn:aws:iam::{{ must_env "AWS_ACCOUNT_ID" }}:role/scheduler
      input: {period: previous}
```

## Libraries available offline

The Python environment provides `boto3`, `yaml` (PyYAML 6.0.3) and
`_gojsonnet` (go-jsonnet 0.22.0 bindings: `evaluate_file`,
`evaluate_snippet`, with `jpathdir` and `native_callbacks`) for reading YAML
and evaluating Jsonnet.

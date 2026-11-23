# account-sweeper: configuration

The configuration is one YAML file, passed with `--config`. These are the top-level
keys in scope:

```yaml
regions:              # regions to sweep; here always exactly [us-east-1]
  - us-east-1

blocklist:            # account IDs that must never be swept; must not be empty
  - "111111111111"

blocklist-terms:      # optional extra alias terms that refuse an account
  - production
no-blocklist-terms-default: false   # true drops the built-in alias term

bypass-alias-check-accounts:        # used only together with --no-alias-check
  - "222222222222"

resource-types:       # global type selection
  includes: [SQSQueue, SSMParameter]
  excludes: []

accounts:             # account ID -> per-account settings
  "123456789012":
    presets: [common]
    resource-types:
      includes: [...]
      excludes: [...]
    filters:
      SQSQueue:
        - ...

presets:              # named, reusable filter sets
  common:
    filters:
      __global__:
        - ...

settings:             # per-resource-type behaviour switches
  KMSKey:
    DeleteInUseKeys: true

endpoints: [...]      # see interface.md
```

Account IDs are strings of digits. Quote them so YAML does not turn them into
numbers.

## Safety gate

Before anything is listed, the program checks the account that the credentials
resolve to. It refuses to run, with a nonzero exit status and no changes, when any
of these holds:

* the blocklist is empty;
* the account ID is on the blocklist;
* the account ID is not a key under `accounts:`;
* the account has no IAM account alias;
* an alias contains a blocklisted term. The built-in term list contains `prod`;
  `blocklist-terms` adds to it, and `no-blocklist-terms-default: true` removes the
  built-in entry.

With `--no-alias-check`, an account listed in `bypass-alias-check-accounts`
skips both the missing-alias check and the alias blocklisted-term check.
The account membership and account blocklist checks still apply.

## Resource-type selection

Includes and excludes can come from the command line (`--include` / `--exclude`),
from the top-level `resource-types` and from the account's `resource-types`. Every
include list that is present narrows the selection further. Any exclude removes
the type. Type names may use `*` and `?` wildcards, for example `KMS*`.

## Filters

Filters decide which discovered resources are **kept** (reported as filtered).
They are keyed by resource type and appear under an account's `filters` or a
preset's `filters`. A resource that matches any applicable filter is kept, unless
filter groups are enabled (see below).

A filter is either a plain string or an object:

```yaml
filters:
  SSMParameter:
    - "/app/keep-me"                 # plain string: exact match
    - property: Name
      type: glob
      value: "/shared/*"
    - property: tag:team
      value: platform
    - type: regex
      value: "^/tmp-[0-9]+$"
  CloudWatchLogsLogGroup:
    - property: CreationTime
      type: dateOlderThan
      value: 24h
    - property: tag:owner
      value: sweeper
      invert: true
```

* `type` is one of `exact` (the default), `contains`, `glob`, `regex`,
  `dateOlderThan` and `dateOlderThanNow`.
  * `glob` uses `*` and `?` wildcards that do not cross `/`.
  * `regex` uses Go regular-expression syntax.
  * The date types take a Go duration (`90m`, `-12h`, `2h45m`) and compare it
    with a timestamp-valued property (any property can hold one, including a
    tag). Accepted timestamp formats include `2006-01-02`, `2006/01/02`,
    RFC 3339 and Unix seconds.
* `property` names one of the bracketed properties shown for the resource in the
  output, for example `Name`, `QueueURL`, `ARN` or `tag:<key>`. Without a
  property (and for a plain string), the filter applies to the resource's
  identifier exactly as it appears in its report line.
* `invert: true` inverts the match result for that filter.
* `group: <name>` puts the filter into a filter group (see below).

### `__global__`

Filters listed under the pseudo resource type `__global__` apply to every resource
type, in addition to that type's own filters.

### Presets

`presets:` defines named filter sets at the top level. An account lists the
presets it uses under `presets:`. Their filters are combined with the account's own
filters.

### Filter groups

With `--feature-flag filter-groups`, filters can carry `group: <name>`. Within one
group, a match by any filter counts as a match for the group. A resource is kept
only when every group matches. Filters without a group belong to a default group.
Without the feature flag, `group` has no effect.

## Settings

`settings` maps a resource type to switches that change how it is removed. For
example, `KMSKey: {DeleteInUseKeys: true}` allows removing KMS keys that other
services still hold grants on, and
`CloudFormationStack: {DisableDeletionProtection: true}` allows removing stacks with
termination protection enabled. Settings are optional.

## Removal

In a real run (`--no-dry-run`) the program asks AWS to delete every resource that
is not filtered. It then lists again, round after round, until each resource is
gone or no longer eligible, retrying failed removals a limited number of times.
Some resources are removed asynchronously in AWS. For example, a KMS key is
scheduled for deletion and stays visible in the `PendingDeletion` state. AWS-managed
items are reported as filtered and never removed. For Secrets Manager, a secret
owned by another AWS service carries the tag `aws:secretsmanager:owningService`
(the emulator accepts this tag when you create a secret, so you can make one).

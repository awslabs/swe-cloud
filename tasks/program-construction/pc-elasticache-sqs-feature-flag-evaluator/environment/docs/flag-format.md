# flag-evaluator — flag format

Each flag is stored as one key in the cache; the value is a JSON object. The flag's
name is the key without the configured prefix.

```json
{
  "variations": {"enabled": true, "disabled": false},
  "targeting": [
    {"name": "staff", "query": "email ew \"@example.com\"", "variation": "enabled"}
  ],
  "defaultRule": {"variation": "disabled"}
}
```

## Flag fields

| Field | Meaning |
|---|---|
| `variations` | Object mapping a variation name to its value. Values may be booleans, strings, numbers, arrays or objects; all variations of a flag are expected to share one type. |
| `targeting` | Optional array of rules, evaluated in order against the evaluation context. |
| `defaultRule` | Rule applied when no targeting rule matches (its `query` is ignored). |
| `disable` | Optional boolean (default `false`); `true` disables the flag. |
| `trackEvents` | Optional boolean (default `true`); `false` suppresses evaluation events for this flag. |
| `version` | Optional string copied into evaluation results and events. |
| `metadata` | Optional object of free-form information about the flag. |
| `bucketingKey` | Optional name of an evaluation-context attribute used instead of the targeting key when splitting traffic. |
| `scheduledRollout` | Optional array of steps that patch the flag from a given date. |
| `experimentation` | Optional `{"start": DATE, "end": DATE}` window during which the flag is active. |

Dates are RFC 3339 strings such as `"2024-01-01T05:00:00Z"`.

## Rules

| Field | Meaning |
|---|---|
| `name` | Optional rule name; used to address the rule from a scheduled step and reported in result metadata. |
| `query` | Condition (mandatory in `targeting` rules). |
| `variation` | Name of the variation to serve. |
| `percentage` | Object mapping variation names to percentages, e.g. `{"a": 20, "b": 80}`. |
| `progressiveRollout` | `{"initial": {"variation", "date", "percentage"?}, "end": {"variation", "date", "percentage"?}}`: traffic shifts from the initial to the end variation between the two dates. |
| `disable` | Optional boolean; a disabled rule is skipped. |

A rule needs at least one of `variation`, `percentage` or `progressiveRollout`; if several
are present the first in the order `progressiveRollout`, `percentage`, `variation` is used.

### Splits and bucketing

Percentage and progressive splits are deterministic. The bucketing value is the
context's targeting key, or, when the flag sets `bucketingKey`, the string value of that
context attribute (dotted names reach nested objects). The subject's bucket is

```
bucket = FNV-1a-32( UTF-8 bytes of flagName + bucketingValue ) mod (P × 1000)
```

where FNV-1a-32 is the 32-bit Fowler–Noll–Vo 1a hash (offset basis `2166136261`,
prime `16777619`) and `P` is the sum of the rule's percentages (`100` for a progressive
rollout). Each variation of a percentage rule owns a contiguous range of buckets whose
width is its percentage × 1000 (percentages may be fractional). How the ranges are laid
out, how a progressive rollout chooses between its two variations over time, and which
flags need a bucketing value at all, are behaviours to observe.

## Scheduled rollout

`scheduledRollout` is an array of steps. Each step has a `date` and any flag fields to
apply from that date on; a step without a date is ignored. Rules inside a step's
`targeting` are matched to existing rules by `name`; fields given in the step replace
the corresponding existing values of that rule. How steps combine with one another, what
happens to step rules whose name matches no existing rule, and which other step fields
show up in evaluation results and events, are behaviours to observe.

```json
"scheduledRollout": [
  {"date": "2020-04-10T00:00:00Z",
   "targeting": [{"name": "beta", "query": "beta eq true", "variation": "on"}]},
  {"date": "2030-01-01T00:00:00Z", "disable": true}
]
```

## Query language

Queries compare evaluation-context attributes with literals:

```
attribute OPERATOR value
```

- Attributes are context field names; nested object fields are reached with dots
  (`company.name`). The targeting key is available as `targetingKey`.
- String literals are double-quoted; numbers, `true`/`false` and lists like
  `["a","b"]` are also accepted.
- Expressions are combined with `and` / `or`, grouped with parentheses and negated with
  `not (...)`. Operators may be written in lower or upper case.

| Operator | Meaning |
|---|---|
| `eq`, `==` | equal |
| `ne`, `!=` | not equal |
| `lt`, `<`, `gt`, `>`, `le`, `<=`, `ge`, `>=` | ordered comparison |
| `co` | contains |
| `sw` | starts with |
| `ew` | ends with |
| `in` | value is in a list |
| `pr` | attribute is present |

Examples:

```
targetingKey eq "aae1cb41"
(email ew "@test.com") and (role eq "backend engineer")
plan in ["pro", "enterprise"]
age ge 18 and not (country eq "FR")
beta pr
```

How attributes of other types, missing attributes, letter case and malformed queries
behave is part of what the service does and is observed through the HTTP API. Queries
written as JSON objects (JsonLogic) are outside the admitted scope.

## Invalid definitions

A stored value that is not a valid flag definition (for example variations of different
types, no `defaultRule`, a rule naming an unknown variation, or an unparsable query)
does not become a flag. How the service treats store values that are not JSON objects
at all is behaviour to observe.

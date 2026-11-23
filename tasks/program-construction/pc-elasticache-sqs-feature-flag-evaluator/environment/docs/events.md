# flag-evaluator — evaluation events on SQS

When an `sqs` exporter is configured, flag evaluations are reported to the queue given
by `queueUrl`. Each SQS message carries one event: the message body is a JSON object and
the message has a string message attribute `emitter`.

## Feature event

```json
{
  "kind": "feature",
  "contextKind": "user",
  "userKey": "user-1",
  "creationDate": 1700000000,
  "key": "my-flag",
  "variation": "on",
  "value": true,
  "default": false,
  "version": "",
  "source": "SERVER"
}
```

| Field | Meaning |
|---|---|
| `kind` | Always `"feature"` for flag evaluations. |
| `contextKind` | Kind of evaluation context (`"user"`, or `"anonymousUser"` when the context's `anonymous` attribute is the boolean `true`). |
| `userKey` | Targeting key of the evaluation context. |
| `creationDate` | Unix time in seconds when the evaluation happened. |
| `key` | Flag name. |
| `variation` | Name of the variation served. |
| `value` | Value served. |
| `default` | `true` when the evaluation failed (an error result), `false` otherwise. |
| `version` | The flag's `version`, or `""`. |
| `source` | `"SERVER"`. |

An optional `metadata` object may also be present; it is outside the admitted scope.

Which requests emit events, how many, and what each event contains for disabled,
missing or failing flags are behaviours to observe. The exporter does not batch:
events are sent shortly after the evaluation that produced them. Observe them with
`receive_message` (long polling, `MessageAttributeNames=["All"]`) and treat the queue
content as an unordered collection.

# flag-evaluator — HTTP API

All endpoints are served on the configured port. Request bodies are JSON
(`Content-Type: application/json`). No authentication is configured in the admitted scope.

## Health

`GET /health` → `200 {"initialized": true}` once the service is serving.

## OFREP evaluation (OpenFeature Remote Evaluation Protocol)

The evaluation context is a JSON object of attributes; `targetingKey` (a string)
identifies the subject. Nested objects are allowed as attribute values. A context whose
`anonymous` attribute is the boolean `true` describes an anonymous subject.

### Single flag

`POST /ofrep/v1/evaluate/flags/{key}`

```json
{"context": {"targetingKey": "user-1", "email": "ann@example.com", "age": 30}}
```

Success (`200`):

```json
{"key": "my-flag", "value": true, "variant": "on", "reason": "TARGETING_MATCH",
 "metadata": {"...": "..."}}
```

Failure: a non-2xx status with

```json
{"key": "my-flag", "errorCode": "FLAG_NOT_FOUND", "errorDetails": "human readable text"}
```

### All flags

`POST /ofrep/v1/evaluate/flags` with the same body returns

```json
{"flags": [{"key": "...", "value": ..., "variant": "...", "reason": "...",
            "metadata": {...}, "errorCode": "...", "errorDetails": "..."}]}
```

with one entry per known flag (`errorCode`/`errorDetails` only when that flag failed).

## Native evaluation API

The context here has the shape `{"key": "user-1", "custom": {...}}`: `key` plays the
role of the targeting key and `custom` holds the other attributes (including
`anonymous`). These endpoints evaluate against the same flags and rules as OFREP.

### Single flag

`POST /v1/feature/{key}/eval`

```json
{"evaluationContext": {"key": "user-1", "custom": {"email": "ann@example.com"}},
 "defaultValue": "fallback"}
```

Response (`200`):

```json
{"value": ..., "variationType": "variation-name", "reason": "...", "errorCode": "",
 "failed": false, "trackEvents": true, "version": "", "cacheable": true, "metadata": {...}}
```

`defaultValue` is the value returned when the flag cannot be evaluated normally.

### All flags

`POST /v1/allflags` with `{"evaluationContext": {...}}` returns

```json
{"valid": true,
 "flags": {"flag-a": {"value": ..., "variationType": "...", "reason": "...", "errorCode": "",
                      "timestamp": 1700000000, "trackEvents": true}}}
```

## Reserved context attribute `gofeatureflag`

An object-valued context attribute named `gofeatureflag` (in OFREP `context`, or in the
native API's `custom`) carries evaluation options rather than subject data:

| Member | Meaning |
|---|---|
| `currentDateTime` | RFC 3339 string; the instant used instead of the current time when evaluating date-dependent flags (scheduled steps, experimentation windows, progressive rollouts). |
| `flagList` | Array of flag names; restricts an OFREP bulk evaluation to those flags. |

## Result vocabulary

`reason` takes values from: `TARGETING_MATCH`, `TARGETING_MATCH_SPLIT`, `SPLIT`,
`DEFAULT`, `STATIC`, `DISABLED`, `ERROR` (plus `UNKNOWN`/`OFFLINE` outside the admitted
scope).

`errorCode` takes values from: `FLAG_NOT_FOUND`, `TYPE_MISMATCH`, `PARSE_ERROR`,
`TARGETING_KEY_MISSING`, `INVALID_CONTEXT`, `GENERAL` (and `PROVIDER_NOT_READY` outside
the admitted scope).

Which reason, variant, value, status code and error code a given flag, context and
request produce, the ordering of bulk results, and the content of `metadata`, are
behaviours to observe.

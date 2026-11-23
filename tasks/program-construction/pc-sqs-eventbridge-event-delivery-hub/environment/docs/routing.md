# event-destination-hub — topics and filters

Each published event is checked against every destination of its tenant. A destination
receives the event only when all of these hold:

1. it is enabled;
2. the event topic matches one of its subscribed topics;
3. the event matches its filter, if it has one.

A publish naming a `destination_id` considers only that destination. Every
matching destination gets an independent delivery.

## Topics

`TOPICS` restricts the topics that can be published or subscribed to. Unset
means any topic. A destination subscribes to a list of topics or to `"*"`
(all topics). With `TOPICS_ALLOW_WILDCARDS=true`, a subscription may contain
`*` inside a string, e.g. `user.*`.

## Filter syntax

A filter is a JSON object matched against the event's top-level properties
`id`, `topic`, `time`, `metadata` and `data`.

- **Exact match:** `{"data": {"status": "paid"}}`. Several keys are combined
  with AND, and nested objects are allowed. An array field matches when it
  contains the given value.
- **Comparison:** `$eq`, `$neq`, `$gt`, `$gte`, `$lt`, `$lte`. Example:
  `{"data": {"amount": {"$gte": 100, "$lt": 500}}}`.
- **Membership:** `$in` (value in array, or substring) and `$nin`.
- **Strings:** `$startsWith`, `$endsWith`.
- **Existence:** `$exist` (`true`/`false`).
- **Logic:** `$or: [...]`, `$and: [...]`, `$not: {...}`.

To remove a filter, PATCH it to `{}`.

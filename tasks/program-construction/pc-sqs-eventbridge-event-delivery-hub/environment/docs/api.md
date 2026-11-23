# event-destination-hub — REST API

Base URL: `http://127.0.0.1:$API_PORT/api/v1`. Send `Authorization: Bearer $API_KEY`
on every request. A tenant token from `GET /tenants/{tenant_id}/token` (a JWT
signed with `API_JWT_SECRET`) may be sent instead. It grants access only to that
tenant's resources and not to the admin-only routes. Request and response bodies
are JSON. Errors are JSON objects with at least `status` and `message`.
Validation errors may also carry `data`.

## Tenants

| Method | Path | Notes |
|--------|------|-------|
| PUT | `/tenants/{tenant_id}` | create or update; body `{"metadata": {"k": "v"}}` (optional) |
| GET | `/tenants/{tenant_id}` | retrieve |
| DELETE | `/tenants/{tenant_id}` | delete tenant and its destinations |
| GET | `/tenants/{tenant_id}/token` | tenant JWT (admin only) |

## Destinations

| Method | Path |
|--------|------|
| GET / POST | `/tenants/{tenant_id}/destinations` |
| GET / PATCH / DELETE | `/tenants/{tenant_id}/destinations/{destination_id}` |
| PUT | `/tenants/{tenant_id}/destinations/{destination_id}/enable` |
| PUT | `/tenants/{tenant_id}/destinations/{destination_id}/disable` |
| GET | `/tenants/{tenant_id}/destinations/{destination_id}/attempts[/{attempt_id}]` |

Create body:

```json
{
  "id": "optional-client-id",
  "type": "aws_sqs",
  "topics": ["user.created"],
  "filter": {"data": {"amount": {"$gte": 100}}},
  "config": {"queue_url": "http://…/000000000000/my-queue", "endpoint": "http://…"},
  "credentials": {"key": "test", "secret": "test"},
  "metadata": {"k": "v"}
}
```

`topics` is a list of topics, or `"*"`/`["*"]` for every topic. PATCH accepts
any subset of these fields. Destination type configuration is described in
[delivery.md](delivery.md). The field schemas are also available at runtime:
`GET /destination-types` and `GET /destination-types/{type}`. `GET /topics`
lists the configured topics.

## Publishing

`POST /publish` (admin only):

```json
{
  "id": "optional-event-id",
  "tenant_id": "t1",
  "destination_id": "optional — deliver only to this destination",
  "topic": "user.created",
  "eligible_for_retry": true,
  "time": "2026-01-01T00:00:00Z",
  "metadata": {"k": "v"},
  "data": {"any": "json object"}
}
```

`tenant_id` and `data` (a JSON object) are required. A successful publish
returns `202` with the event id, a `duplicate` flag and the matched destination IDs.

## Event and attempt queries

| Method | Path |
|--------|------|
| GET | `/events`, `/events/{event_id}` |
| GET | `/attempts`, `/attempts/{attempt_id}` |
| POST | `/retry` — body `{"event_id": "...", "destination_id": "..."}` |

Admin requests scope the list endpoints with `tenant_id`. The filter parameters are:

- `/events`: `id`, `topic`, `destination_id`
- `/attempts`: `event_id`, `destination_id`, `destination_type`, `status` (`success`/`failed`), `topic`, `include` (`event`, `event.data`, `destination`)

Time ranges use `time[gte]`, `time[lte]`, `time[gt]` and `time[lt]` (RFC 3339 or a date).
A repeated parameter matches any of its values.

Lists return `{"models": [...], "pagination": {...}}`. Page with `limit`, the
`next`/`prev` cursors from `pagination`, `order_by` and `dir` (`asc`/`desc`).

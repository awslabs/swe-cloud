# list-mailer: HTTP API reference (admitted scope)

All request and response bodies are JSON unless stated otherwise. Successful
responses are wrapped as `{"data": <value>}`; errors as `{"message": "<text>"}`
with a 4xx/5xx status. IDs in paths are positive integers. Paginated listings
accept `page` and `per_page` (`per_page=all` for everything) and return
`{"results": [...], "total": N, "per_page": N, "page": N, ...}`.

## Health and settings

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/health` | `{"data": true}` when ready. Authenticated like every `/api/*` route. |
| GET | `/api/settings` | Full settings object, flat dotted keys (e.g. `upload.provider`). Secret values are masked. |
| PUT | `/api/settings` | Body: the complete settings object (start from GET and modify). Triggers an in-place reload. Unknown keys are ignored. An empty string for a secret (e.g. `upload.s3.aws_secret_access_key`) keeps the stored secret. |

Settings relevant to the admitted scope:

* `upload.provider` (`filesystem` or `s3`), `upload.extensions` (allowed file
  extensions, `*` for any), `upload.s3.*` (see `interface.md`).
* `bounce.enabled`, `bounce.webhooks_enabled` (the bounce webhook endpoint
  exists only while `bounce.webhooks_enabled` is on; it records bounces only
  when `bounce.enabled` is on too), `bounce.actions`: an object
  keyed by bounce type (`soft`, `hard`, `complaint`), each
  `{"count": <int>, "action": "none" | "unsubscribe" | "blocklist" | "delete"}`.
* `smtp`: array of SMTP server objects (`host`, `port`, `enabled`, ...).
* `privacy.domain_blocklist`, `privacy.domain_allowlist`: arrays of e-mail
  domains (`*.example.com` style wildcards allowed) that restrict which
  addresses subscriber operations accept.
* `privacy.*`, `app.*`: other behavior switches; inspect GET output.

## Lists

| Method | Path | Body / query |
| --- | --- | --- |
| GET | `/api/lists` | `query` (name search), `type`, `optin`, `status`, `tag` (repeatable), `order_by` (`name`, `status`, `created_at`, `updated_at`, `subscriber_count`), `order` (`asc`/`desc`), `page`, `per_page`, `minimal`. |
| GET | `/api/lists/{id}` | |
| POST | `/api/lists` | `name` (required), `type` (`public`/`private`/`temporary`), `optin` (`single`/`double`), `status` (`active`/`archived`), `tags` (string array), `description`. |
| PUT | `/api/lists/{id}` | Same fields as POST. |
| DELETE | `/api/lists/{id}` | |
| DELETE | `/api/lists` | Query: `id` (repeatable), or `query` with optional `all=true`. |

## Subscribers

| Method | Path | Body / query |
| --- | --- | --- |
| GET | `/api/subscribers` | `query` (SQL expression, see `querying.md`), `search`, `list_id` (repeatable), `subscription_status`, `order_by` (`email`, `name`, `status`, `created_at`, `updated_at`), `order`, `page`, `per_page`. |
| GET | `/api/subscribers/{id}` | Includes `lists` with each `subscription_status`. |
| POST | `/api/subscribers` | `email` (required), `name`, `status` (`enabled`/`disabled`/`blocklisted`), `lists` (list IDs), `attribs` (JSON object), `preconfirm_subscriptions` (bool). |
| PUT | `/api/subscribers/{id}` | Same fields as POST (full update). |
| PATCH | `/api/subscribers/{id}` | Any subset of the POST fields. |
| DELETE | `/api/subscribers/{id}` | |
| DELETE | `/api/subscribers` | Query: `id` (repeatable). |
| PUT | `/api/subscribers/{id}/blocklist` | |
| PUT | `/api/subscribers/blocklist` | `{"ids": [...]}` |
| PUT | `/api/subscribers/lists` | `ids`, `action` (`add`/`remove`/`unsubscribe`), `target_list_ids`, optional `status` (`confirmed`/`unconfirmed`/`unsubscribed`) for `add`. |
| PUT | `/api/subscribers/query/lists` | `query`, `search`, `list_ids`, `subscription_status`, `action`, `target_list_ids`, optional `status` (same values; `all: true` targets every subscriber). |
| PUT | `/api/subscribers/query/blocklist` | `query`, `search`, `list_ids`, `subscription_status`, `all`. |
| POST | `/api/subscribers/query/delete` | `query`, `search`, `list_ids`, `subscription_status`, `all`. |
| GET | `/api/subscribers/{id}/bounces` | Bounce records of one subscriber. |
| DELETE | `/api/subscribers/{id}/bounces` | |

Subscriber `status` is the subscriber-wide state; each list membership has its
own subscription status (`unconfirmed`, `confirmed`, `unsubscribed`).

For bulk `add`, new memberships default to `unconfirmed` when `status` is
omitted. Adding by IDs preserves an existing membership's status when
`status` is omitted and updates it when supplied. Adding by query preserves
existing memberships even when `status` is supplied; the supplied status
applies only to new memberships.

## Import

| Method | Path | Body |
| --- | --- | --- |
| POST | `/api/import/subscribers` | `multipart/form-data` with `params` (JSON string) and `file` (CSV, or a ZIP containing CSV files). |
| GET | `/api/import/subscribers` | `{"name", "total", "imported", "status"}`; status is one of `none`, `importing`, `stopping`, `finished`, `failed`. |
| GET | `/api/import/subscribers/logs` | Import log text. |
| DELETE | `/api/import/subscribers` | Stop/clear the current import. |

`params` fields: `mode` (`subscribe` or `blocklist`, required), `delim` (single
character, required), `lists` (list IDs), `subscription_status`
(`confirmed`/`unconfirmed`/`unsubscribed`), `overwrite_userinfo` (bool: update
existing subscribers' name and attributes), `overwrite_subscription_status`
(bool: update existing memberships' status), `overwrite` (bool: shorthand
that turns both overwrite switches on).
Only one import runs at a time; imports run in the background, poll the status.

CSV format: first row is a header. Columns `email` (required) and `name`; an
optional `attributes` column holding a JSON object; other columns are ignored.
Example (comma delimiter):

```
email,name,attributes
ann@example.com,Ann,"{""city"": ""Oslo"", ""score"": 3}"
ben@example.com,Ben,"{}"
```

## Bounces

| Method | Path | Body / query |
| --- | --- | --- |
| POST | `/webhooks/bounce` | `email` or `subscriber_uuid` (one required), `campaign_uuid`, `source` (optional; stored as `""` when omitted), `type` (`soft`/`hard`/`complaint`, required), `meta` (JSON), `created_at`. Authenticated like `/api/*`. |
| GET | `/api/bounces` | `campaign_id`, `source`, `order_by` (`email`, `campaign_name`, `source`, `created_at`, `type`), `order`, `page`, `per_page`. |
| GET | `/api/bounces/{id}` | |
| DELETE | `/api/bounces/{id}` | |
| DELETE | `/api/bounces` | Query: `id` (repeatable) or `all=true`. |
| PUT | `/api/bounces/blocklist` | Blocklist all subscribers that have bounce records. |

## Media

| Method | Path | Body |
| --- | --- | --- |
| POST | `/api/media` | `multipart/form-data` with one `file` field. |
| GET | `/api/media` | `query` (filename search), `page`, `per_page`. |
| GET | `/api/media/{id}` | |
| DELETE | `/api/media/{id}` | Removes the record and the stored object(s). |

Image uploads (e.g. PNG, JPEG, GIF) get a generated thumbnail stored
alongside the original. Allowed file extensions come from
`upload.extensions` (`*` allows any).

## Response objects

Fields compared by graders (other fields may be present):

* List: `id`, `uuid`, `name`, `type`, `optin`, `status`, `tags` (array, `[]` when none),
  `description`. (`subscriber_count`, `subscriber_statuses` are cached and
  not compared.)
* Subscriber: `id`, `uuid`, `email`, `name`, `attribs`, `status`, `lists`:
  one entry per membership with the list's `id`, `name`, `type`, `optin` and
  the membership's `subscription_status`.
* Bounce: `id`, `type`, `source`, `meta`, `created_at`, `email`,
  `subscriber_uuid`, `subscriber_id`, `subscriber_status`, `campaign`
  (`{"id", "name"}` or `null`).
* Media: `id`, `uuid`, `filename`, `content_type`, `provider`, `meta`, `url`,
  `thumb_url` (`null` when there is no thumbnail).
* Import status: `name`, `total`, `imported`, `status`.
* Write operations without a natural result answer `{"data": true}`.

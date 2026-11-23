# GC Notify API — what this service uses

Condensed from the GC Notify documentation (send, status and callbacks pages). Base URL is
`$NOTIFY_API_URL`; every request carries `Authorization: ApiKey-v1 <key>`.

## Send an email

```
POST /v2/notifications/email
{"email_address": "...", "template_id": "...", "personalisation": {...}, "reference": "..."}
```

`201 Created` with `{"id": "<notification uuid>", "reference": "...", "content": {...}, ...}`.

- Every accepted request is a new notification and a new email. Notify does not deduplicate:
  sending the same request twice emails the owner twice, whatever the `reference`.
- `reference` is optional and free text chosen by the caller (no personal information). It
  identifies a single notification or a batch of notifications and is returned on every status
  read and callback. We put the submission's id in it so a notification can be traced back.
- The notification exists from the moment Notify has accepted the request. If the connection
  fails or times out before the response is read, the email may well have been sent.
- Notify accepts a request (creates the notification) as soon as an API server picks it up,
  before the slow part: rendering the template and handing the email to the delivery queue,
  which is what a slow `201` is waiting on. A request that has not been accepted within 2 seconds
  of the caller sending it never will be: the API gateway drops requests that wait longer than
  that for a server, and a dropped request creates nothing. So a notification that is going to
  exist is listed by reference (below) at most 2 seconds after its request was sent, even while
  its sender is still waiting for the `201` or is no longer there to read it.

Error responses are JSON `{"status_code": ..., "errors": [...]}` with 400 (validation), 403
(auth), 429 (rate limit, see Limits) or 500.

## Get the status of one notification

```
GET /v2/notifications/{notification_id}
```

`200` with the notification (`id`, `reference`, `email_address`, `type`, `status`,
`created_at`, ...) or `404` (no such notification, or one older than the retention period).

## Get the status of multiple notifications

```
GET /v2/notifications?reference=<reference>&status=<status>&template_type=email
```

All arguments optional. Returns `{"notifications": [...]}`, newest first, up to 250 per page.
`reference` is an exact match on the value given at send time; every notification carrying that
reference is returned, whatever its status, as long as it is within the retention period (see
Limits). A notification is listed as soon as it has been
created, before it has been delivered, and whether or not the sender ever read the `201`.

## Statuses

| status | meaning | resend? |
|---|---|---|
| `created`, `sending`, `pending` | in transit | no |
| `delivered` | delivered to the recipient | no |
| `permanent-failure` | address wrong or blocked; remove it | no |
| `temporary-failure` | could not deliver, e.g. inbox full; "you can try to send the message again" | yes |
| `technical-failure` | not sent because of a problem between Notify and the provider; "you'll have to try sending again" | yes |

## Delivery-status callbacks

Notify POSTs a JSON receipt to our callback URL with `Authorization: Bearer <callback token>`:

```
{"id": "<notification id>", "reference": "<reference as sent>", "to": "...",
 "status": "...", "status_description": "...", "provider_response": null,
 "created_at": "...", "completed_at": "...", "sent_at": "...", "notification_type": "email"}
```

- **Multiple callbacks for one notification.** You might receive several callbacks for one sent
  notification (for example accepted, then bounced). Callbacks are sent in the order they are
  received.
- **Retries.** A callback that does not get a 2xx is retried — up to 6 times over 25 minutes in
  production — so the same receipt can arrive more than once, and late.
- **Response time.** The endpoint must answer within 1 second; a slower answer counts as a
  failure and is retried.
- Notify also sends `{"health_check": "true"}` to the URL when callbacks are configured.

## Limits

- **Rate limit.** Restrict requests to GC Notify's API to 1000 per minute: at most 1000 requests
  per 60 seconds per API key, counting every call, sends and status reads alike. Over the limit
  Notify answers `429` with `RateLimitError` ("Exceeded rate limit for key type LIVE of 1000
  requests per 60 seconds") and does not process the request; Notify treats it as unprocessed, so
  a send that got a `429` created nothing. Every worker and the callback server use the same key.
- **Data retention.** You can only get the status of messages that are 7 days old or newer. Older
  notifications are no longer returned by the list call, and `GET /v2/notifications/{id}` answers
  `404` for them.

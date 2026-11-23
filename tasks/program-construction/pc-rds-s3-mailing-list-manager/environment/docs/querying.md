# list-mailer: querying subscribers

Several subscriber endpoints accept a `query` parameter: an SQL boolean
expression evaluated by PostgreSQL against the `subscribers` table (aliased
`subscribers`). Any expression valid in a `WHERE` clause over that table works,
including JSONB operators on `subscribers.attribs`:

```
subscribers.email LIKE '%@example.com'
subscribers.attribs->>'city' = 'Oslo'
(subscribers.attribs->>'score')::INT > 5
subscribers.attribs->'tags' ? 'vip'
subscribers.status = 'blocklisted'
subscribers.created_at > NOW() - INTERVAL '1 day'
```

`search` is a free-text search over names/e-mails instead of SQL. `list_id`
(GET) / `list_ids` (bulk bodies) restrict matching to subscribers on those
lists, optionally further restricted by `subscription_status`.

An expression that PostgreSQL cannot evaluate is reported as an HTTP error.

Subscriber `attribs` is an arbitrary JSON object (nested objects and arrays
allowed); it is stored as JSONB.

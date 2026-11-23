# Product request — destination availability during consumer failures

Filed against the ordered tier after incident 4712. Not yet accepted.

Destinations stop accepting deliveries all the time: consumer deploys, short
outages, rate limits. Today one failing event holds up every later event for
that destination for as long as it keeps failing, which customers read as the
whole destination being down.

## Requested behaviour

While a destination is failing some of its events:

- later events that would succeed should still be delivered; and
- the failing event is delivered once the destination recovers.

Product's reading: this should be compatible with the ordered tier, because
each event is still delivered exactly once and the failing event is not lost —
it is only delivered after recovery.

## What is needed before it can ship

An engineering decision, recorded in `/app/decision.json` as
`decision.schema.json` describes: whether the requested behaviour and the
ordered tier's contract (`service.md`) can both hold for one destination, and
which behaviour the service implements once the question is settled. Quote what
you actually observed when you answer it.

Whatever the decision, the incident stands on its own: the ordered tier is
misbehaving today, and that is fixed independently of this request.

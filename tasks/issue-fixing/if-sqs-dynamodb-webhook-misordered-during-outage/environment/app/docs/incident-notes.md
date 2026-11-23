# Incident 4712 — ordered destinations delivering out of order

Collected by on-call, window 02:10–02:40 UTC. Two consumer reports against the
ordered tier, and the platform timeline for the same window.

## Consumer report 1 — destination `dst-4f2a` (their ingest log)

The consumer's endpoint returned 503 for roughly ninety seconds during their
own deploy. Their ingest log for the window, abridged:

```
02:11:40  evt-2f9c-0001  ack
02:11:52  evt-2f9c-0003  ack
02:11:52  evt-2f9c-0004  ack
02:11:53  evt-2f9c-0005  ack
02:11:54  evt-2f9c-0006  ack
02:11:54  evt-2f9c-0007  ack
02:11:55  evt-2f9c-0008  ack
02:12:31  evt-2f9c-0002  ack        <- after the endpoint recovered
```

They expect `0002` before `0003`. Two of their downstream consumers reconcile
on sequence and both flagged the window.

## Consumer report 2 — destination `dst-91d7` (no errors at all)

No failures on either side during the window. Their ingest log shows one
swapped adjacent pair, `evt-6b31-0009` before `evt-6b31-0008`, in an otherwise
clean and in-order run of twenty-four events. Both were acknowledged.

## Platform timeline for the window

```
02:11:38  dispatcher pod restarted (memory limit); replacement picked up the
          destination's pending stream and the stream drained normally
02:11:5x  deliveries to dst-4f2a continuing while dst-4f2a's endpoint was
          still failing
02:12:29  dst-4f2a endpoint recovery
```

The queue for `dst-4f2a` drained by 02:12:40 with no events left pending; both
consumers eventually received every event exactly once each. The problem is the
order things arrived in, and what the stream was holding while it happened.

## What we are asking for

Fix the service so the ordered tier behaves like the ordered tier. Product has
also filed `request-destination-availability.md` against the same service; that
request needs a decision before anyone acts on it. The decision goes in
`/app/decision.json` as `decision.schema.json` describes.

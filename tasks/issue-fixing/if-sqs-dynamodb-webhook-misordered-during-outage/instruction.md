# Incident 4712 — the ordered tier is delivering out of order

You are on call for the delivery platform. Two consumers of the ordered tier
have reported that events arrived out of order. The evidence, as the operators
collected it, is in `docs/incident-notes.md`.

The service is `/app/dispatcher.py`; its contract is `docs/service.md`. It
reads a run configuration, takes a destination's events off its pending stream,
delivers them over HTTP and reports to the control plane. The platform runs it
as `python3 /app/dispatcher.py --config <run-config.json>`; `localrun.py`
stands up a throwaway stream and drives one clean pass locally. SQS is on
`localhost:4566`, DynamoDB alongside it.

## What has to hold afterwards

- The ordered tier behaves like the ordered tier. Everything the contract in
  `docs/service.md` states must actually be true of the service — read it as
  the acceptance criteria.
- The product request in `docs/request-destination-availability.md` is
  settled: `/app/decision.json`, in the shape `docs/decision.schema.json`
  describes, answering whether that request and the ordered tier's contract
  can both hold for one destination, which behaviour the service implements,
  and why — citing what you observed, not what the request asserts.

## Constraints

- The service's externally visible behaviour is the platform contract: the
  control-plane announcements in `docs/service.md` and their positions in the
  flow stay as they are. A rewrite is fine; silence on those calls is not.
- The pending stream is SQS and stays SQS: the platform inspects a
  destination's stream directly, configures the queue itself, and will not
  accept a reconfigured one (delays, retention changes, redrive).
- Deliveries remain `POST /deliver/{endpoint_id}` with `events` as documented,
  at most `delivery_batch_size` per request. How many you send per request is
  yours to choose.
- Repair the service in place under `/app`. The decision record is
  `/app/decision.json`.

You have a real terminal, the emulator is running, and everything in `/app`
and `docs/` is yours to read, run and change.

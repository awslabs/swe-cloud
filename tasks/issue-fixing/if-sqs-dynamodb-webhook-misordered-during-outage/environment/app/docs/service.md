# Ordered-destination delivery service

The service delivers events to destinations. A destination is one consumer
endpoint; events are accepted for it in order and must reach it in that order.
The pending stream is one SQS FIFO queue per destination run: every event is
one message in one message group, so the stream hands events over in the order
they were accepted.

## The contract

These are the properties the platform holds a destination to. They hold at all
times, including while a destination is rejecting deliveries.

1. **Order.** A consumer never sees event *N+1* before event *N* has been
   acknowledged. Deliveries to one destination are made in acceptance order.
2. **One delivery in flight.** At most one delivery to a destination may be
   awaiting the receiver's acknowledgement at any moment. The next delivery
   starts only after the previous one is acknowledged.
3. **Acknowledged means done.** An event whose delivery the receiver
   acknowledged (a 2xx response) is delivered once and is never sent again.
   Every other outcome — 4xx, 5xx, 3xx, timeout — is a failure.
4. **Undelivered work stays on the stream.** The stream is the only record of
   what has not been delivered. The platform may stop the service process and
   start a replacement at any moment; nothing outside the stream survives that,
   and a replacement picks up exactly the work the stream still holds, in
   order. One consequence is accepted: if the process is stopped after the
   receiver acknowledged an event but before the service finished its
   disposition of it, the stream still holds that event and the replacement
   delivers it once more. That is the only repeat the platform tolerates; a
   running service never repeats an acknowledged event.
5. **No receive past unresolved work.** While a delivery the service has taken
   is unresolved, the service does not take further events off the stream for
   that destination.
6. **Visibility is never extended.** A receive's visibility timeout is
   `visibility_seconds` and the service never extends it.

## Deliveries

`POST {receiver_url}/deliver/{endpoint_id}` with body
`{"events": [ {event_id, seq, payload_nonce, payload}, ... ]}`. The platform
accepts at most `delivery_batch_size` events in one request and treats it as
one delivery: the receiver acknowledges the whole request or fails the whole
request. How many events the service puts in one request is its own choice, up
to that maximum; the response delay is per request, not per event.

`delivery_batch_size` is also the largest receive the service asks the stream
for.

## Control plane

The service reports its progress to `{control_url}`. These announcements are
part of the platform contract: a rewrite of the service must keep making them,
from the same points in the flow.

| Call | When | Body |
|---|---|---|
| `POST /ready` | once, before any stream access | `{pid, run_id, phase}` |
| `GET /go?run_id=&phase=` | polled until released (within `barrier_seconds`) | — |
| `POST /after-response` | after every receiver response, before the service does anything with the received messages | `{run_id, phase, endpoint_id, request_id, status, message_ids, receipt_handles, seqs}` |
| `POST /after-disposition` | after the service has finished doing whatever it does with those messages (deleting, putting back, re-adding), and before it retries or takes more events | `{run_id, phase, request_id}` |
| `POST /done` | when the service considers the run's work complete | `{run_id, phase, delivered}` |

Answers are actions. `CONTINUE` carries on. `RELOAD` tells the service to drop
whatever it is holding and take the stream from the top again. The control
plane may hold a call for a long stretch — tens of seconds — before answering;
read timeouts on these calls must allow for that.

## Configuration

Written by the platform per run; the service takes its path as
`--config <path>`.

| Key | Meaning |
|---|---|
| `run_id`, `phase` | identifies the run and the phase within it |
| `endpoint_id` | the destination; also the stream's message group |
| `queue_url` | the destination's pending stream |
| `receiver_url`, `control_url` | where to deliver and report |
| `delivery_batch_size` | max events per request and max receive size |
| `http_timeout_seconds` | receiver call timeout |
| `poll_wait_seconds` | stream long-poll wait |
| `retry_backoff_seconds` | pause after a failed delivery |
| `visibility_seconds` | per-receive visibility, never extended |
| `barrier_seconds` | budget for the ready/go barrier |

Credentials and the region come with the config (`aws` block) or from the
environment.

## Running it

```
python3 /app/dispatcher.py --config <run-config.json>   # one run, started by the platform
python3 /app/localrun.py                                # one clean local pass, 24 events
```

`localrun.py` stands up a throwaway queue, a recording receiver and an idle
control plane, runs one full pass and prints counts. It is a smoke run: it does
not exercise destinations that fail. Writing a receiver that fails some events,
and stopping and replacing the service mid-run, is straightforward — the
protocols above are the whole surface.

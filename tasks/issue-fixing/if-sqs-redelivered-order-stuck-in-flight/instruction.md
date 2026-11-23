# Orders stuck "in flight" after the fulfilment slowdown

`fulfilment-relay` (`/app`, see `/app/README.md`) drains our `orders` SQS standard queue and calls
the fulfilment service once per order.

Last Tuesday fulfilment had a slow spell. Some calls took about 40 seconds, and for part of that
window the relay's visibility heartbeats for those orders weren't getting through to SQS. SQS
redelivered those orders while they were still being worked on. That is at-least-once delivery
and we expect it. What happened next is the bug:

- Two orders never left the queue. `ApproximateNumberOfMessagesNotVisible` sat at 2 for hours.
  The relay kept calling `ChangeMessageVisibility` for both every few seconds, long after both
  fulfilment calls had returned.
- One of the two had been fulfilled. It was never deleted.
- The other one's fulfilment call had failed. It was never retried, and it never reached the
  dead-letter queue either.
- Restarting the relay "fixed" it. Both orders came back and were processed again, so the
  fulfilled one was shipped twice.

Please fix the relay so that an order redelivered while it is still in progress is handled
correctly. Afterwards:

- An order never has two fulfilment calls open at the same time.
- When fulfilment succeeds, the order is deleted and not worked on again.
- When fulfilment fails, the order goes back on the queue after
  `REDELIVER_VISIBILITY_TIMEOUT_SECONDS` and is retried.
- An order that keeps failing is still attempted on each receive. It ends up in the dead-letter
  queue through the queue's redrive policy, and only through it.
- Once the work for an order is finished, nothing about it stays in flight.
- Other orders keep flowing while some are slow.

Constraints:

- The relay stays one long-running process started as `/app/run-consumer` and configured through
  the environment as the README describes. Keep the fulfilment request format.
- It is a consumer only. It must not send messages, change queue attributes, or create, purge or
  move queues. It talks to SQS only through `SQS_ENDPOINT_URL`.
- Restarting or re-executing itself is not a fix.
- SQS responses in our region can be slow; a call can take a couple of seconds to come back. The
  relay has to be correct however long a call takes to return.

An SQS emulator is running at `http://localhost:4566` for trying things out (the README shows how
to create a queue and run the relay against it).

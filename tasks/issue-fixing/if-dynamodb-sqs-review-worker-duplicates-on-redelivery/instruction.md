# Reviews lost, doubled or half-composed since compose-review moved to spot capacity

Since the compose-review worker pool (`/app`) moved onto spot instances, support keeps getting
reports we can't explain from the code paths we've looked at:

- a review never shows up on its movie page, although all five of its upload parts were queued;
- a review shows up with a field missing;
- the same review is listed twice on a movie page;
- a review is on its movie page but never reaches the author's review history (the user-review
  service never heard of it);
- `review_compose` items reappear for requests that finished long ago, holding one part and
  `counter` 1;
- the upload replies the frontend logs don't add up: a part replying `rank` 6, or two parts of one
  submission both replying `composed: true`.

Spot reclaims kill a worker without warning, at any point between two AWS calls, and the reply to
the call it was making can be lost even though the call was applied. The message it was working on
comes back from SQS after the queue's visibility timeout and is processed again by whichever worker
gets it, often a fresh replacement, while the request's other parts keep being processed. Some
instances hibernate instead of dying: the worker freezes and later carries on from where it stopped,
usually after its message has been processed elsewhere. Last sprint on-call wrapped the handler in
an idempotency record (`idempotency.py`); the reports kept coming.

Please fix the compose-review worker so that reviews come out right under these conditions, without
breaking moderation: a review they have taken down must stay down. `/app/README.md` describes the
service, its tables, the reply contract the frontend relies on, moderation, and how the pool is
operated.

The fix goes out by replacing the pool in place: the current workers are stopped between messages
and yours start on the same queue and tables, which are neither drained nor migrated. Your workers
will therefore find requests that the current code has partly processed or finished, and the
frontend's retried sends keep arriving for both. The current pool is reclaimed like any other right
up to the switch, so an upload it was in the middle of when its worker was lost comes back to yours
from SQS, with whatever that upload had already written.

One pre-switch case is out of scope: a current-pool worker lost in the middle of composing, after it
stored the review but before it cleaned up the request's `review_compose` item. The tables can't
tell whether that review was ever on its movie page, or was listed and already taken down, so
operations reconciles those few by hand from the old pool's logs; don't guess either way for them.

Constraints:

- Keep the entrypoint `python3 /app/compose_worker.py --worker-id <id>` and the environment
  variables it reads. The pool manager starts each worker as a fresh process in its own empty
  working directory and replaces lost workers under new ids; workers share no local disk.
- Every AWS call goes to the endpoint in `AWS_ENDPOINT_URL`.
- The queues and the frontend's tables belong to the deployment: don't change the queues'
  attributes or add a redrive policy, and don't delete items the frontend created other than the
  `review_compose` cleanup the service already does.
- `/app/tools/` has a local stack and a submit script for trying the normal path against the local
  emulator.

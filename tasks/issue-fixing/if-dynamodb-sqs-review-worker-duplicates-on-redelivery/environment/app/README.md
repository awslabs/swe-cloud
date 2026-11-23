# reelnotes — compose-review workers

The review pipeline behind movie pages. When a user submits a review, the frontend splits it into
five **upload parts** and queues one SQS message per part. A pool of compose-review workers
consumes them. Each upload records its part on the request's `review_compose` item and bumps the
item's counter. The upload that completes the set **composes** the review: it stores the review,
puts it on the movie's page, cleans up the request's `review_compose` item and hands the review to
the user-review service.

```
frontend ──UploadReq──▶ review_compose[req_id] = {counter: 0}
         ──5 messages─▶ SQS upload-parts ──▶ compose_worker.py (pool of 3)
                                               │ upload(): part + counter
                                               │ compose (completing upload only)
                                               │   ├─ reviews_store[review_id]
                                               │   ├─ movie_reviews[movie_id].review_ids += [review_id]
                                               │   ├─ cleanup: delete review_compose[req_id]
                                               │   └─ SQS user-review-uploads ──▶ user-review service
                                               └ reply: responses[req_id, part]
```

## Code

| file | |
|---|---|
| `compose_worker.py` | entrypoint and receive loop: `python3 /app/compose_worker.py --worker-id <id>` |
| `compose_review.py` | the upload handlers and the compose step (port of the Go ComposeReview service) |
| `idempotency.py` | handler-level idempotency records |
| `settings.py` | environment configuration, part names |
| `tools/` | local stack, a submit script and moderation's takedown, see *Running it locally* |

## Messages

Standard queue `upload-parts` (`QUEUE_URL`), one message per part:

```json
{"req_id": "req-3f1c…", "part": "rating", "value": "8", "movie_id": "tt0062622"}
```

`part` is one of `unique_id`, `text`, `rating`, `user_id`, `movie_id`. The `unique_id` part's value
is the review id. Every part of a request carries the same `movie_id`. A message's body is the same
on every delivery.

## Tables

All created by the deployment; names come from the environment.

| env | default | key | written by |
|---|---|---|---|
| `TABLE_COMPOSE` | `review_compose` | `req_id` | frontend creates `{req_id, counter: 0}`; each upload adds its part as an attribute named after the part (string) and increments `counter`; compose deletes it |
| `TABLE_RESPONSES` | `responses` | `req_id`, `part` | each upload's reply |
| `TABLE_REVIEWS` | `reviews_store` | `review_id` | compose: `{review_id, req_id, unique_id, text, rating, user_id, movie_id}`, `req_id` being the request whose compose stored it |
| `TABLE_MOVIES` | `movie_reviews` | `movie_id` | frontend creates `{movie_id, review_ids: []}`; compose appends the review id; moderation removes ids it takes down |
| `TABLE_IDEMPOTENCY` | `compose_idempotency` | `id` | the workers |

`review_compose.counter` is the number of distinct parts of the request that have been uploaded;
it reaches 5 exactly when all five parts are on the item. `review_compose` holds in-flight requests
only: the compose step removes the request's item (the Go service's `Cleanup`), and a finished
request leaves no item behind. The movie page renders `review_ids` in order, so a review id must
appear there once.

## User-review service

Users' review-history pages belong to the user-review service, run by another team. Composing a
review hands it over with one message on that service's standard queue (`USER_REVIEW_QUEUE_URL`,
the Go service's asynchronous `UploadUserReview` call):

```json
{"review_id": "rv-…", "user_id": "user-…", "movie_id": "tt0062622", "rating": "8"}
```

Values are the review's own, as strings. When the user-review service processes a message it reads
the review from `reviews_store`; a message whose review is not there is discarded as bogus. It
records each `review_id` once, so a repeated message is harmless. Nothing else tells that service
about a review.

## Moderation

Moderators take a review down by removing its id from `movie_reviews[movie_id].review_ids`
(`tools/take_down.py` makes the same call their console makes). The `reviews_store` item stays, and
so does the review in the author's history on the user-review service. A takedown can come at any
time once a review is on its page, within seconds of the listing when the automated filter flags it.
A review that was taken down stays off its movie page: nothing in this pipeline puts it back.

## Replies

Each upload's reply is its return value to the frontend, written to `responses[req_id, part]`
before the message is deleted:

| attribute | type | meaning |
|---|---|---|
| `rank` | N | the value of the counter that this upload's own increment produced: 1 for the first part of the request to land, 5 for the part that completed it |
| `composed` | BOOL | true exactly for the upload whose increment completed the set (rank 5); that upload is the one that composes the review |

So for one request the five replies carry ranks 1..5 once each, and exactly one says
`composed: true`. The frontend may read a reply as soon as it appears. A redelivered or re-sent
upload must produce the same reply its first execution produced.

## Operations

- The pool runs on spot capacity. A worker can be reclaimed at any moment — between any two AWS
  calls — with no shutdown hook. The reply to the call it was making may never arrive even though
  the call was applied.
- Part of the spot capacity hibernates instead of terminating: the worker's process is frozen between
  two AWS calls (or while a call is in flight) and resumes where it stopped, memory and connections
  intact, when capacity comes back, typically tens of seconds later. The reply to its last call
  arrives then. By that time its message has usually been redelivered and the request finished by
  other workers; the pool manager counts a hibernated worker as lost and has already replaced it.
- The pool manager starts each worker as a fresh process in its own empty working directory and
  replaces a lost worker after about a second, under a new id. Workers share no disk.
- SQS redelivers a message that was received and not deleted once the queue's visibility timeout
  (6 s) has passed; any worker, including a replacement, may receive it.
- The frontend retries a `SendMessage` whose reply it did not get, so the same part of a request can
  occasionally be queued twice (identical body, different `MessageId`), sometimes seconds apart and
  after the request has finished. Duplicates are treated as the same upload.
- When a submission's replies are slow to appear, the frontend may resubmit it: it creates a new
  request (new `req_id`, its own `review_compose` item) and queues five new messages carrying the
  same values, the review id included, while the first request is still being processed. Both
  requests then run to the end. Each is a request like any other, with its own five replies under
  the rules in *Replies* and its own completing upload; the review they carry is still one review,
  stored once, listed once and handed over.
- SLO: once a request's five parts are queued, the review is composed (stored, listed, handed to the
  user-review service, its `review_compose` item cleaned up) and all five replies are written within
  30 seconds, including when a worker is lost mid-upload.
- The queue's attributes (visibility timeout, redrive) belong to the platform team and are not
  changed from this repository. Apart from the compose step's cleanup of the request's
  `review_compose` item, workers do not delete items the frontend creates. Workers may add their own
  attributes to `review_compose` items and may create additional tables at startup.
- Every AWS call goes to the endpoint in `AWS_ENDPOINT_URL`.

## Running it locally

The dev container runs a local AWS emulator on `http://localhost:4566` (`AWS_ENDPOINT_URL` is set).

```bash
eval "$(python3 /app/tools/dev_stack.py)"          # queues + tables, prints QUEUE_URL, USER_REVIEW_QUEUE_URL
python3 /app/compose_worker.py --worker-id dev-1 &
python3 /app/tools/submit_review.py                # one review, then its replies and state
python3 /app/tools/take_down.py <movie_id> <review_id>   # what a moderator's takedown does
```

Emulator note: the local emulator does not implement `ClientRequestToken` idempotency on
`TransactWriteItems`; a repeated token is applied again.

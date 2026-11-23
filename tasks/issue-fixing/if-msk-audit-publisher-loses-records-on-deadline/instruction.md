Audit ingress is reporting successful publishes for events that our downstream
MSK audit reader cannot find. It has shown up around broker maintenance and
request cancellations; a normal low-load run looks healthy. The service now pipelines several calls
on each partition, and later calls can still be outstanding when one is canceled. We also cannot
solve this by letting request handlers wait indefinitely: the ingress deadline
is part of the API used by the frontends.

Repair the Python service in `/app`. Preserve the HTTP contract and the deployment
constraints in `/app/README.md`. After the fix, every successful response must
refer to its own event in the broker log, events must not be duplicated, and
traffic on either partition must keep working across these transient failures.
Calls which fail still have to return on time and stop sending that event.

`python3 /app/provision.py` provisions a development MSK broker and prints its
bootstrap address; `/app/README.md` explains how to start the service. Production
and verification supply `KAFKA_BOOTSTRAP`, `TOPIC` and `PORT` and launch
`python3 /app/publisher.py`. Work on the implementation; no deployment is needed.

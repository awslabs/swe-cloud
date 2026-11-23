The event sink has started leaving gaps in search during transport incidents. Operators
also found repeated business keys after retrying deliveries and replacing failed
processes. The incident became more frequent after adding a second intake worker. This service writes through a rolling OpenSearch target; restarting it
sometimes clears the backlog and sometimes loses accepted work.

Repair the service in `/app`. Accepted events must keep flowing, with one stored copy per
business key, including when an earlier write's outcome was unknown. Permanent mapping
rejections still belong in the S3 dead-letter destination. The existing delivery SLO is
25 seconds, including search visibility; do not trade the gaps for an indefinite backlog.

The service entry point is `python -m sink`. `/app/README.md` contains its deployment
contract and development setup. Keep that interface and the managed-store restrictions.
The evaluator supplies a fresh domain, transport endpoint and DLQ bucket, starts workers on their configured ports, and exercises delivery, concurrent redelivery,
transport incidents and independent worker replacement against the same durable inbox.
Accepted work must survive those replacements while peers continue serving intake.

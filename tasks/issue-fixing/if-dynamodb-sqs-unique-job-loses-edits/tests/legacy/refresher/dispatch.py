"""Dispatch a refresh for one entity, unless one is already waiting in the queue."""

import json

from . import config
from .jobs import RefreshEntity
from .locks import LockStore
from .unique import UniqueLock


def dispatch(entity_id, delay=0, unique_for=None, cfg=None):
    cfg = cfg or config.load()
    job = RefreshEntity(entity_id=entity_id,
                        unique_for=cfg.unique_for if unique_for is None else unique_for)
    unique = UniqueLock(LockStore(config.dynamodb(), cfg.unique_table))
    if not unique.acquire(job):
        return {"status": "DROPPED", "entity": entity_id}
    sent = config.sqs().send_message(QueueUrl=cfg.queue_url, MessageBody=job.to_body(),
                                     DelaySeconds=delay)
    return {"status": "ACCEPTED", "entity": entity_id, "message_id": sent["MessageId"]}


def main(args):
    print(json.dumps(dispatch(args.entity, delay=args.delay, unique_for=args.unique_for)),
          flush=True)
    return 0

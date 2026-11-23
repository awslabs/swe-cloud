import functools
import json
import time
from decimal import Decimal
from aws import payload_key
from persistence import Conflict, RecordStore


class AlreadyInProgress(Exception):
    pass


def idempotent(handler):
    @functools.wraps(handler)
    def wrapped(event, ctx):
        key = payload_key(event)
        store = RecordStore()
        try:
            store.acquire(key, ctx, event.get('dispatch_keys', []))
        except Conflict as conflict:
            record = conflict.record
            if (record.get('status', {}).get('S') == 'COMPLETED'
                    and Decimal(record['expiration']['N']) > int(time.time())):
                return json.loads(record['response_data']['S'])
            raise AlreadyInProgress(key)
        try:
            result = handler(event, ctx)
        except Exception:
            store.delete(key, ctx)
            raise
        try:
            store.complete(key, ctx, result)
        except Conflict as exc:
            raise AlreadyInProgress(key) from exc
        return result
    return wrapped

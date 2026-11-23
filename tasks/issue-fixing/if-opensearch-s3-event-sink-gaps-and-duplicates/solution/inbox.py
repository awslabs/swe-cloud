"""Shared intake ownership and per-key recovery in a conditional S3 catalog."""
import hashlib
import json
import threading

from botocore.exceptions import ClientError
from . import config
from .storage import Storage


class Inbox(Storage):
    def __init__(self):
        super().__init__()
        self.lock = threading.RLock()
        self.records = {}
        self.first_attempts = set()
        self.key_locks = {}
        for number in range(32):
            page, _ = self.read_page(number)
            self.records.update({term: record for term, record in page['entries'].items()
                                 if record['owner'] == config.WORKER_ID})

    def page_number(self, term):
        return hashlib.sha256(str(term).encode()).digest()[0] % 32

    def page_key(self, number):
        return config.INBOX_PREFIX + 'catalog/%02d.json' % number

    def read_page(self, number):
        response = self.client.get_object(Bucket=config.INBOX_BUCKET, Key=self.page_key(number))
        return json.loads(response['Body'].read()), response['ETag']

    def key_lock(self, term):
        with self.lock:
            return self.key_locks.setdefault(term, threading.RLock())

    def mutate(self, term, fields=None, document=None):
        number = self.page_number(term)
        while True:
            page, etag = self.read_page(number)
            entries = page['entries']
            previous = entries.get(term)
            if fields is None:
                if previous is not None:
                    return previous, False
                record = {'event': document, 'phase': 'attempt', 'owner': config.WORKER_ID}
            else:
                if previous['owner'] != config.WORKER_ID:
                    raise RuntimeError('Cannot change peer delivery state')
                record = {**previous, **fields}
            # The ETag belongs to this entire page, not to this process's cached
            # records. Preserve unrelated keys written by peers in that page.
            entries[term] = record
            try:
                self.client.put_object(Bucket=config.INBOX_BUCKET, Key=self.page_key(number),
                                       Body=json.dumps(page).encode(), ContentType='application/json',
                                       IfMatch=etag)
            except ClientError as exc:
                if exc.response['ResponseMetadata']['HTTPStatusCode'] not in (409, 412):
                    raise
                self.reset_client()
                continue
            return record, True

    def accept(self, document):
        term = document[config.QUERY_TERM]
        with self.key_lock(term):
            if term in self.records:
                return False
            record, created = self.mutate(term, document=document)
            if record['owner'] != config.WORKER_ID:
                return False
            # Stable slot ownership covers even a successful creation whose
            # response was lost before it could be published to a local queue.
            with self.lock:
                self.records[term] = record
            if record['phase'] == 'attempt':
                self.first_attempts.add(term)
            return created or record['phase'] != 'done'

    def update(self, document, **fields):
        term = document[config.QUERY_TERM]
        with self.key_lock(term):
            record, _ = self.mutate(term, fields=fields)
            with self.lock:
                self.records[term] = record

    def all(self):
        with self.lock:
            return list(self.records.values())

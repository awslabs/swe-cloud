"""S3 intake catalog, partitioned into independently stored JSON pages."""
import hashlib
import json
import threading

from . import config
from .storage import Storage


class Inbox(Storage):
    def __init__(self):
        super().__init__()
        self.lock = threading.RLock()
        self.records = {}
        self.key_locks = {}
        self.pages = {}
        self.page_locks = [threading.RLock() for _ in range(32)]
        for number in range(32):
            page = json.loads(self.client.get_object(
                Bucket=config.INBOX_BUCKET, Key=self.page_key(number))['Body'].read())
            self.pages[number] = page
            self.records.update(page['entries'])

    def page_number(self, term):
        return hashlib.sha256(str(term).encode()).digest()[0] % 32

    def page_key(self, number):
        return config.INBOX_PREFIX + 'catalog/%02d.json' % number

    def key_lock(self, term):
        with self.lock:
            return self.key_locks.setdefault(term, threading.RLock())

    def save(self, term, record):
        number = self.page_number(term)
        with self.page_locks[number]:
            page = {'entries': {**self.pages[number]['entries'], term: record}}
            self.client.put_object(Bucket=config.INBOX_BUCKET, Key=self.page_key(number),
                                   Body=json.dumps(page).encode(), ContentType='application/json')
            self.pages[number] = page
            with self.lock:
                self.records[term] = record

    def accept(self, document):
        term = document[config.QUERY_TERM]
        with self.key_lock(term):
            if term in self.records:
                return False
            self.save(term, {'event': document, 'phase': 'queued'})
            return True

    def update(self, document, **fields):
        term = document[config.QUERY_TERM]
        with self.key_lock(term):
            self.save(term, {**self.records[term], **fields})

    def all(self):
        with self.lock:
            return list(self.records.values())

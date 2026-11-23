import json
import logging
import threading
import time
from dataclasses import dataclass

from . import config
from .transport import request

log = logging.getLogger(__name__)


@dataclass
class Pending:
    document: dict
    start: float


class QueryManager:
    def __init__(self, ready, inbox=None):
        self.ready = ready
        self.inbox = inbox
        self.pending = {}
        self.lock = threading.RLock()
        self.last_query_time = time.monotonic()

    def add(self, document):
        with self.lock:
            group = self.pending.setdefault(config.WRITE_ALIAS, {})
            group[document[config.QUERY_TERM]] = Pending(document, time.monotonic())

    def run(self, stop):
        while not stop.wait(config.QUERY_INTERVAL_S):
            try:
                self.query_once()
            except Exception:
                log.exception('Query cycle failed; retrying')

    def query_once(self):
        with self.lock:
            if not any(self.pending.values()):
                return
            lines = []
            for index, entries in self.pending.items():
                terms = list(entries)
                for offset in range(0, len(terms), 1000):
                    chunk = terms[offset:offset + 1000]
                    lines.extend([json.dumps({'index': index}), json.dumps({
                        'size': len(chunk) * 2, '_source': [config.QUERY_TERM],
                        'query': {'terms': {config.QUERY_TERM: chunk}}})])
            response = request('POST', '/_msearch', data='\n'.join(lines) + '\n',
                               headers={'Content-Type': 'application/x-ndjson'})
            response.raise_for_status()
            for result in response.json()['responses']:
                if 'error' in result:
                    log.warning('Search failed: %s', result['error'])
                    continue
                for hit in result['hits']['hits']:
                    term = hit['_source'][config.QUERY_TERM]
                    group = self.pending[hit['_index']]
                    group.pop(term, None)
            for group in self.pending.values():
                for term, entry in list(group.items()):
                    if entry.start + config.QUERY_DURATION_S < self.last_query_time:
                        self.ready.put(entry.document)
                        del group[term]
            self.last_query_time = time.monotonic()

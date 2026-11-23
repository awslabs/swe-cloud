import json
import logging
import queue
import threading
import time
from dataclasses import dataclass

from . import config
from .transport import request

log = logging.getLogger(__name__)


@dataclass(eq=False)
class Pending:
    document: dict
    apply_by: float


def seconds(value):
    for unit, factor in [('ms', .001), ('s', 1), ('m', 60), ('h', 3600)]:
        if value.endswith(unit):
            result = float(value[:-len(unit)]) * factor
            if result > 0:
                return result
    raise ValueError('Unbounded refresh interval: ' + value)


class QueryManager:
    def __init__(self, ready, inbox):
        self.ready, self.inbox = ready, inbox
        self.pending = {}
        self.dead_letters = queue.Queue()
        self.lock = threading.RLock()

    def recover(self, incoming):
        # A durable attempt can have reached the engine even if its response and
        # all in-memory callbacks died. Reestablish uncertainty on every restart.
        now = time.monotonic()
        for record in self.inbox.all():
            document = record['event']
            phase = record['phase']
            if phase == 'queued':
                incoming.put(document)
            elif phase in ('attempt', 'lookup'):
                self.pending[document[config.QUERY_TERM]] = Pending(
                    document, now + config.MAX_LATE_APPLY_S)
            elif phase == 'rejected':
                self.dead_letters.put((document, record['error']))

    def claim(self, document):
        term = document[config.QUERY_TERM]
        with self.inbox.key_lock(term):
            record = self.inbox.records[term]
            initial = term in self.inbox.first_attempts
            if record['phase'] != 'queued' and not (initial and record['phase'] == 'attempt'):
                return False
            self.inbox.first_attempts.discard(term)
            if document.get('potential_duplicate') and not record.get('checked'):
                self.add(document)
                return False
            # Persist BEFORE dispatch. 'queued' can be replayed immediately;
            # 'attempt' must be reconciled after any interruption.
            # Intake already journaled a conservative attempt before any peer
            # can acknowledge it. First dispatch needs no second S3 round trip.
            if not initial:
                self.inbox.update(document, phase='attempt')
            return True

    def add(self, document):
        term = document[config.QUERY_TERM]
        with self.inbox.key_lock(term):
            self.inbox.update(document, phase='lookup')
            with self.lock:
                self.pending[term] = Pending(document, time.monotonic() + config.MAX_LATE_APPLY_S)

    def retry(self, document):
        with self.inbox.key_lock(document[config.QUERY_TERM]):
            self.inbox.update(document, phase='queued', checked=True)
            self.ready.put(document)

    def complete(self, document):
        term = document[config.QUERY_TERM]
        with self.inbox.key_lock(term):
            self.inbox.update(document, phase='done')
            with self.lock:
                self.pending.pop(term, None)

    def rejected(self, document, error):
        with self.inbox.key_lock(document[config.QUERY_TERM]):
            # Keep the store's original rejection across a crash before S3 put.
            self.inbox.update(document, phase='rejected', error=error)
            self.dead_letters.put((document, error))

    def run(self, stop):
        while not stop.wait(.25):
            try:
                self.query_once()
            except Exception:
                log.exception('Query cycle failed; retrying')

    def query_once(self):
        with self.lock:
            snapshot = list(self.pending.items())
        if not snapshot:
            return
        response = request('GET', '/' + config.WRITE_ALIAS + '/_settings')
        response.raise_for_status()
        settings = response.json()
        if not settings:
            raise ValueError('Empty settings response')
        refresh = max(seconds(s['settings']['index'].get('refresh_interval', '1s'))
                      for s in settings.values())
        lines, searches = [], []
        for offset in range(0, len(snapshot), 1000):
            chunk = snapshot[offset:offset + 1000]
            searches.append(chunk)
            lines.extend([json.dumps({'index': config.WRITE_ALIAS}), json.dumps({
                'size': len(chunk), '_source': [config.QUERY_TERM],
                'collapse': {'field': config.QUERY_TERM},
                'query': {'terms': {config.QUERY_TERM: [term for term, _ in chunk]}}})])
        started = time.monotonic()
        response = request('POST', '/_msearch', data='\n'.join(lines) + '\n',
                           headers={'Content-Type': 'application/x-ndjson'})
        response.raise_for_status()
        results = response.json()['responses']
        if len(results) != len(searches):
            raise ValueError('incomplete msearch response')
        for group, result in zip(searches, results):
            if ('error' in result or result.get('timed_out') or result.get('terminated_early')
                    or result.get('_shards', {}).get('failed', 0)):
                continue
            found = {hit['_source'][config.QUERY_TERM] for hit in result['hits']['hits']}
            for term, entry in group:
                with self.inbox.key_lock(term):
                    with self.lock:
                        if self.pending.get(term) is not entry:
                            continue
                    if term in found:
                        self.complete(entry.document)
                    elif started >= entry.apply_by + refresh:
                        # Persist safe retry before publishing it to a worker.
                        self.retry(entry.document)
                        with self.lock:
                            del self.pending[term]

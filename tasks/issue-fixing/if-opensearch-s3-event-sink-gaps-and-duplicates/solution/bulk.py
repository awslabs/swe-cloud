import json
from concurrent.futures import ThreadPoolExecutor
import logging
import queue
import threading
import time

import boto3
from . import config
from .transport import request

log = logging.getLogger(__name__)


class BulkWorker:
    def __init__(self, incoming, ready, queries, inbox):
        self.incoming, self.ready, self.queries = incoming, ready, queries
        self.inbox = inbox
        self.s3 = boto3.client('s3')

    def send(self, documents):
        lines = []
        for document in documents:
            lines.extend([json.dumps({'index': {'_index': config.WRITE_ALIAS}}), json.dumps(document)])
        try:
            response = request('POST', '/_bulk', data='\n'.join(lines) + '\n',
                               headers={'Content-Type': 'application/x-ndjson'})
            if response.status_code in (500, 504):
                for document in documents:
                    self.queries.add(document)
                return
            response.raise_for_status()
            items = response.json()['items']
            if len(items) != len(documents):
                raise ValueError('incomplete bulk response')
        except Exception:
            log.warning('Bulk outcome unknown; submitting for lookup', exc_info=True)
            for document in documents:
                self.queries.add(document)
            return
        for document, wrapper in zip(documents, items):
            item = next(iter(wrapper.values()))
            status = item['status']
            if 200 <= status < 300:
                self.queries.complete(document)
                continue
            if status in (500, 504):
                self.queries.add(document)
            elif 400 <= status < 500 and status != 429:
                self.queries.rejected(document, item.get('error'))
            else:
                self.queries.retry(document)

    def dispatch(self, documents):
        try:
            self.send(documents)
        except Exception:
            log.exception('Bulk processing failed')

    def deliver_dead_letters(self, stop):
        while not stop.is_set():
            try:
                document, error = self.queries.dead_letters.get(timeout=.1)
            except queue.Empty:
                continue
            try:
                self.s3.put_object(Bucket=config.DLQ_BUCKET,
                                   Key=config.DLQ_PREFIX + document[config.QUERY_TERM] + '.json',
                                   Body=json.dumps({'event': document, 'error': error}).encode(),
                                   ContentType='application/json')
                self.queries.complete(document)
            except Exception:
                log.exception('Dead letter not complete; retrying')
                self.queries.dead_letters.put((document, error))
                stop.wait(.25)

    def run(self, stop):
        threading.Thread(target=self.deliver_dead_letters, args=(stop,), daemon=True).start()
        executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='bulk')
        try:
            self.collect(stop, executor)
        finally:
            executor.shutdown(wait=True)

    def collect(self, stop, executor):
        while not stop.is_set() or not self.incoming.empty() or not self.ready.empty():
            documents = []
            until = time.monotonic() + config.FLUSH_INTERVAL_S
            while len(documents) < config.BULK_MAX_DOCS:
                try:
                    document = self.ready.get_nowait()
                except queue.Empty:
                    try:
                        document = self.incoming.get(timeout=max(0.001, until - time.monotonic()))
                    except queue.Empty:
                        break
                if not self.queries.claim(document):
                    if time.monotonic() >= until:
                        break
                    continue
                documents.append(document)
                if time.monotonic() >= until:
                    break
            if documents:
                executor.submit(self.dispatch, documents)

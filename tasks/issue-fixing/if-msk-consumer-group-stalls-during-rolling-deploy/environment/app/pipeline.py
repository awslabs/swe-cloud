"""HTTP enrichment followed by durable publication to the receipts topic.

The HTTP request and output envelope are part of the ingestion interface. An empty
processor URL uses the development SHA-256 transform. Production supplies the URL.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import urllib.request
from confluent_kafka import Producer


class Pipeline:
    def __init__(self, bootstrap, output_topic, processor_url, instance):
        self.producer = Producer({'bootstrap.servers': bootstrap})
        self.output_topic, self.processor_url, self.instance = output_topic, processor_url, instance
        self.pool = ThreadPoolExecutor(max_workers=8)
        self.pending = {}
        self.safe = {}
        self.limit = 16

    def install(self, offsets):
        self.safe.update(offsets)

    def enrich(self, envelope):
        if self.processor_url:
            request = urllib.request.Request(self.processor_url.rstrip('/')+'/enrich',
                data=json.dumps(envelope).encode(), headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read())
        return {'sha256': hashlib.sha256(base64.b64decode(envelope['value'])).hexdigest()}

    def submit(self, record):
        tp = record.topic(), record.partition()
        key = tp + (record.offset(),)
        envelope = dict(topic=tp[0], partition=tp[1], offset=record.offset(),
                        value=base64.b64encode(record.value()).decode(), instance_id=self.instance)
        self.pending[key] = dict(envelope=envelope, future=self.pool.submit(self.enrich, envelope),
                                 published=False, acknowledged=False, error=None)

    def tick(self):
        self.producer.poll(0)
        for key, job in list(self.pending.items()):
            if job['error']: raise RuntimeError(job['error'])
            if not job['published'] and job['future'].done():
                output = {**job['envelope'], 'result': job['future'].result()}
                def delivered(error, message, job=job):
                    job['error'], job['acknowledged'] = error, error is None
                self.producer.produce(self.output_topic, key=json.dumps(key).encode(),
                                      value=json.dumps(output).encode(), on_delivery=delivered)
                job['published'] = True
            if job['acknowledged']:
                self.safe[key[:2]] = max(self.safe.get(key[:2], 0), key[2]+1)
                del self.pending[key]

    def can_accept(self): return len(self.pending) < self.limit

    def close(self):
        self.pool.shutdown(wait=True)
        self.producer.flush(10)

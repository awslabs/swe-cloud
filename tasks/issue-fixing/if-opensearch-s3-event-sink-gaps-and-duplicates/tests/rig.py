"""Independent wire fault controller and resource provisioning."""
import json
import os
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

import boto3
import requests
from botocore.config import Config


def http(method, url, **kw):
    with requests.Session() as session:
        session.trust_env = False
        return session.request(method, url, timeout=15, **kw)


def provision():
    aws = os.environ.get('AWS_ENDPOINT_URL', 'http://engines:4566')
    tag = uuid.uuid4().hex[:12]
    search = boto3.client('opensearch', endpoint_url=aws, region_name='us-east-1',
                          config=Config(read_timeout=600, retries={'max_attempts': 1}))
    name = 'verify-' + tag
    search.create_domain(DomainName=name, EngineVersion='OpenSearch_2.11',
                         ClusterConfig={'InstanceType': 't3.small.search', 'InstanceCount': 1})
    for _ in range(180):
        info = search.describe_domain(DomainName=name)['DomainStatus']
        if info.get('Endpoint') and not info.get('Processing', False):
            break
        time.sleep(1)
    else:
        raise RuntimeError('domain unavailable')
    endpoint = info['Endpoint']
    if not endpoint.startswith('http'):
        endpoint = 'http://' + endpoint
    for _ in range(90):
        try:
            http('GET', endpoint).raise_for_status()
            break
        except requests.RequestException:
            time.sleep(1)
    else:
        raise RuntimeError('engine endpoint unavailable')
    body = {'index_patterns': ['events-*'], 'template': {
        'settings': {'number_of_shards': 1, 'number_of_replicas': 0, 'refresh_interval': '5s'},
        'mappings': {'dynamic': 'strict', 'properties': {
            'key': {'type': 'keyword'}, 'ts': {'type': 'date'},
            'payload': {'type': 'keyword'}, 'potential_duplicate': {'type': 'boolean'}}}}}
    http('PUT', endpoint + '/_index_template/events', json=body).raise_for_status()
    http('PUT', endpoint + '/events-000001', json={
        'aliases': {'events': {'is_write_index': True}}}).raise_for_status()
    s3 = boto3.client('s3', endpoint_url=aws, region_name='us-east-1')
    bucket = 'verify-dlq-' + tag
    s3.create_bucket(Bucket=bucket)
    return endpoint, s3, bucket


class Wire:
    def __init__(self, endpoint, triggers, logpath):
        self.endpoint, self.triggers = endpoint, triggers
        self.lock = threading.Lock()
        self.fired, self.groups, self.records, self.errors = set(), {}, [], []
        self.late_done = threading.Event()
        self.logpath = logpath
        self.server = None

    def log(self, **record):
        record['time'] = time.monotonic()
        with self.lock:
            self.records.append(record)
            with open(self.logpath, 'a') as stream:
                stream.write(json.dumps(record) + '\n')

    def forward(self, method, path, body, content_type):
        response = http(method, self.endpoint + path, data=body,
                        headers={'Content-Type': content_type})
        if path.split('?')[0].rstrip('/').endswith('_bulk'):
            result = response.json()
            self.log(kind='applied', statuses=[next(iter(i.values()))['status']
                                              for i in result.get('items', [])])
        return response

    def late(self, body, content_type):
        try:
            # Reserve one second inside the eight-second apply bound for engine I/O.
            time.sleep(7)
            self.forward('POST', '/_bulk', body, content_type).raise_for_status()
        except Exception as exc:
            self.errors.append('delayed forward: ' + repr(exc))
        finally:
            self.late_done.set()

    def intercept_bulk(self, documents, body, content_type):
        return None

    def start(self, port=9201):
        wire = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def answer(self, status, body=b'{}', content_type='application/json'):
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def handle_request(self):
                try:
                    self.process()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except Exception as exc:
                    wire.errors.append('proxy: ' + repr(exc))
                    self.answer(502)

            def process(self):
                body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                content_type = self.headers.get('Content-Type', 'application/json')
                parsed = urlsplit(self.path)
                path = parsed.path.rstrip('/') or '/'
                params = parse_qs(parsed.query)
                forbidden = (self.command == 'DELETE' or
                             any(x in path for x in ('_refresh', '_flush', '_delete_by_query', '_update_by_query')) or
                             any(v in ('true', 'wait_for') for v in params.get('refresh', [])))
                bulk = path.endswith('/_bulk') or path == '/_bulk'
                operations, documents = [], []
                if bulk:
                    try:
                        lines = [json.loads(line) for line in body.splitlines() if line.strip()]
                        assert len(lines) % 2 == 0
                        operations, documents = lines[::2], lines[1::2]
                        forbidden |= len(documents) > 10
                        for operation in operations:
                            forbidden |= list(operation) != ['index']
                            metadata = next(iter(operation.values()))
                            forbidden |= '_id' in metadata or metadata.get('_index', 'events') != 'events'
                    except Exception:
                        forbidden = True
                # Collection API is deliberately read-only except index bulks and searches.
                if self.command not in ('GET', 'HEAD'):
                    forbidden |= not (bulk or path.endswith('/_search') or path.endswith('/_msearch')
                                      or path in ('/_search', '/_msearch'))
                if forbidden:
                    wire.log(kind='forbidden', method=self.command, path=self.path)
                    return self.answer(400, b'{"error":"managed collection restriction"}')
                rule = None
                if bulk:
                    keys = [d['key'] for d in documents]
                    wire.log(kind='bulk', keys=keys)
                    with wire.lock:
                        for candidate, key in wire.triggers.items():
                            if key in keys and candidate not in wire.fired:
                                wire.fired.add(candidate)
                                wire.groups[candidate] = keys
                                rule = candidate
                                break
                if bulk:
                    intercepted = wire.intercept_bulk(documents, body, content_type)
                    if intercepted is not None:
                        return self.answer(*intercepted)
                if rule == 'one':
                    time.sleep(.5)
                    self.close_connection = True
                    try:
                        self.connection.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    self.connection.close()
                    threading.Thread(target=wire.late, args=(body, content_type), daemon=True).start()
                    return
                if rule == 'three':
                    return self.answer(500, b'{"error":"internal_server_error","status":500}')
                response = wire.forward(self.command, self.path, body, content_type)
                if rule == 'two':
                    response.raise_for_status()
                    if not wire.late_done.wait(20):
                        raise RuntimeError('delayed request not finished before rollover')
                    rolled = http('POST', wire.endpoint + '/events/_rollover', json={})
                    rolled.raise_for_status()
                    wire.log(kind='rollover', result=rolled.json())
                    return self.answer(500, b'{"error":"internal_server_error","status":500}')
                if rule == 'four':
                    result = response.json()
                    for document, item in zip(documents, result['items']):
                        if document['key'] == wire.triggers['four']:
                            item['index'] = {'status': 500, 'error': {
                                'type': 'internal_server_error', 'reason': 'transient backend error'}}
                    result['errors'] = True
                    return self.answer(200, json.dumps(result).encode())
                return self.answer(response.status_code, response.content,
                                   response.headers.get('Content-Type', 'application/json'))

            do_GET = handle_request
            do_HEAD = handle_request
            do_POST = handle_request
            do_PUT = handle_request
            do_DELETE = handle_request

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return 'http://127.0.0.1:' + str(self.server.server_port)

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class RecoveryWire(Wire):
    """Protocol gates survive process replacement; they are not sink callbacks."""
    def __init__(self, endpoint, logpath, originals):
        super().__init__(endpoint, {}, logpath)
        self.originals = set(originals)
        self.stage = 1
        self.applied_keys = None
        self.retry_keys = None
        self.first_applied = threading.Event()
        self.retry_waiting = threading.Event()
        self.release_first = threading.Event()
        self.release_retry = threading.Event()
        self.late_done = threading.Event()

    def intercept_bulk(self, documents, body, content_type):
        keys = {document['key'] for document in documents}
        with self.lock:
            stage = self.stage
            first = stage == 1 and self.applied_keys is None
            if first:
                self.applied_keys = keys
            retry = (stage == 2 and self.retry_keys is None and
                     bool(keys & (self.originals - self.applied_keys)))
            if retry:
                self.retry_keys = keys
        if stage == 1:
            if first:
                self.forward('POST', '/_bulk', body, content_type).raise_for_status()
                self.log(kind='boundary', stage=1, keys=sorted(keys), applied=True)
                self.first_applied.set()
            else:
                self.log(kind='boundary', stage=1, keys=sorted(keys), applied=False)
            if not self.release_first.wait(40):
                raise RuntimeError('first replacement never released the transport')
            return 500, b'{"error":"outcome unknown","status":500}'
        if retry:
            self.log(kind='boundary', stage=2, keys=sorted(keys), applied=False)
            # Count from receipt, independently of the controller's kill. A
            # client with a short read timeout can observe failure before that
            # kill; the eight-second bound must hold for that client as well.
            threading.Thread(target=self.late, args=(body, content_type), daemon=True).start()
            self.retry_waiting.set()
            if not self.release_retry.wait(40):
                raise RuntimeError('second replacement never released the transport')
            return 500, b'{"error":"outcome unknown","status":500}'
        return None


def prepare_inbox(s3, bucket, prefix):
    # Ordinary deployment provisioning, also performed by public bootstrap.
    for number in range(32):
        s3.put_object(Bucket=bucket, Key=prefix + 'catalog/%02d.json' % number,
                      Body=b'{"entries":{}}', ContentType='application/json')

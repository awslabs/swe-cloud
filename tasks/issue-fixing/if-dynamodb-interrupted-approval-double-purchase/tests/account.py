"""Verifier-owned account, independent of all /app modules."""
import base64
import copy
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import boto3


def scan(client, table, **kwargs):
    items = []
    while True:
        page = client.scan(TableName=table, ConsistentRead=True, **kwargs)
        items.extend(page.get('Items', []))
        if not page.get('LastEvaluatedKey'):
            return items
        kwargs['ExclusiveStartKey'] = page['LastEvaluatedKey']


def keyed(items, key):
    return {item[key]['S']: item for item in items}


class Account:
    def __init__(self, client, journal_path):
        self.db = client
        self.lock = threading.RLock()
        self.secret = secrets.token_bytes(32)
        self.journal_path = journal_path
        self.latest = {'commitments': {}, 'provider_tokens': {}}
        self.records = {}
        self.journal = []
        self.tokens = {}
        self.events = []
        self.tamper = []
        self.phase = 'approval'
        self.rules = {}
        self.server = None

    def ref(self, value):
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def image(self, key_name, key, record):
        # Account records are represented by opaque, fixed-size receipts. Private
        # account metadata is in the journal, never exposed via a table Scan.
        return {key_name: {'S': key}, 'receipt': {'S': base64.b64encode(secrets.token_bytes(768)).decode()},
                'seal': {'S': self.ref(uuid.uuid4().hex)},
                'tag_refs': {'L': [{'S': self.ref(k + '\0' + v)} for k, v in sorted(record.get('tags', {}).items())]}}

    def audit(self):
        with self.lock:
            expected = {'commitments': {}, 'provider_tokens': {}}
            for event in self.journal:
                for change in event['images']:
                    expected[change['table']][change['key']] = change['image']
            for table, key in [('commitments', 'commitment_ref'), ('provider_tokens', 'token_ref')]:
                try:
                    actual = keyed(scan(self.db, table), key)
                except Exception as exc:
                    self.tamper.append(f'{table} unavailable: {exc}')
                    continue
                if actual != expected[table]:
                    self.tamper.append(f'{table} diverged from account journal')
            return not self.tamper

    def persist(self, writes, kind, record):
        transaction = []
        for table, key_name, key, image in writes:
            old = self.latest[table].get(key)
            put = dict(TableName=table, Item=image)
            if old:
                put.update(ConditionExpression='seal=:seal', ExpressionAttributeValues={':seal': old['seal']})
            else:
                put['ConditionExpression'] = f'attribute_not_exists({key_name})'
            transaction.append({'Put': put})
        self.db.transact_write_items(TransactItems=transaction)
        event = {'at': time.time(), 'kind': kind, 'record': copy.deepcopy(record), 'images': []}
        for table, _, key, image in writes:
            self.latest[table][key] = copy.deepcopy(image)
            event['images'].append({'table': table, 'key': key, 'image': image})
        with open(self.journal_path, 'a') as handle:
            handle.write(json.dumps(event) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        self.journal.append(copy.deepcopy(event))

    def purchase(self, body):
        processing_deadline = time.monotonic() + 15
        kind = body['provider_type']
        if kind not in ('token', 'tag_after', 'none') or not isinstance(body['count'], int) or body['count'] <= 0:
            raise ValueError('invalid product or count')
        allowed = {'provider_type', 'offering_id', 'count'}
        if kind == 'token':
            allowed |= {'client_token', 'tags'}
        if set(body) - allowed:
            raise ValueError('unsupported fields for product')
        offering = body['offering_id']
        with self.lock:
            phase = self.phase
            rule = self.rules.get((phase, offering), '')
            self.events.append(dict(kind='arrival', offering_id=offering, phase=phase, at=time.monotonic()))
        if rule == 'hold_before_commit':
            self.park(offering, phase, rule)
            time.sleep(15)
            raise ValueError('request discarded')
        if rule == 'delay_then_commit':
            time.sleep(3)
        with self.lock:
            self.audit()
            if time.monotonic() >= processing_deadline:
                raise ValueError('purchase processing limit exceeded; request discarded')
            token = body.get('client_token', '') if kind == 'token' else ''
            previous = self.tokens.get(token) if token else None
            if previous and time.time() - previous['at'] < 20:
                cid = previous['commitment_id']
            else:
                cid = 'c-' + uuid.uuid4().hex
                record = dict(commitment_id=cid, provider_type=kind, offering_id=offering,
                              count=body['count'], tags=body.get('tags', {}).copy(), created_at=int(time.time() * 1000))
                key = self.ref(cid)
                image = self.image('commitment_ref', key, record)
                writes = [('commitments', 'commitment_ref', key, image)]
                if token:
                    token_key = self.ref(token)
                    token_image = self.image('token_ref', token_key, {})
                    writes.append(('provider_tokens', 'token_ref', token_key, token_image))
                self.persist(writes, 'purchase', record)
                self.records[cid] = record
                if token:
                    self.tokens[token] = dict(at=time.time(), commitment_id=cid)
        if rule == 'commit_then_hold':
            self.park(offering, phase, rule)
            time.sleep(15)
        return {'commitment_id': cid}

    def park(self, offering, phase, rule):
        with self.lock:
            self.events.append(dict(kind='parked', offering_id=offering, phase=phase, rule=rule, at=time.monotonic()))

    def describe(self, query):
        kind = query['provider_type']
        if kind not in ('token', 'tag_after'):
            raise ValueError('describe unsupported')
        ref = self.ref(query['tag_key'] + '\0' + query['tag_value'])
        with self.lock:
            self.audit()
            images = scan(self.db, 'commitments', FilterExpression='contains(tag_refs, :ref)',
                          ExpressionAttributeValues={':ref': {'S': ref}})
            keys = {row['commitment_ref']['S'] for row in images}
            return [{key: record[key] for key in ('commitment_id', 'offering_id', 'tags', 'created_at')}
                    for cid, record in self.records.items()
                    if self.ref(cid) in keys and record['provider_type'] == kind
                    and record['tags'].get(query['tag_key']) == query['tag_value']]

    def tag(self, body):
        with self.lock:
            self.audit()
            cid = body['commitment_id']
            record = copy.deepcopy(self.records[cid])
            if record['provider_type'] != 'tag_after':
                raise ValueError('tag unsupported')
            record['tags'].update(body['tags'])
            key = self.ref(cid)
            self.persist([('commitments', 'commitment_ref', key, self.image('commitment_ref', key, record))], 'tag', record)
            self.records[cid] = record
        return {}

    def start(self):
        account = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def dispatch(self):
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    body = json.loads(self.rfile.read(length)) if length else None
                    path = urlparse(self.path)
                    if self.command == 'POST' and path.path == '/purchase':
                        result = account.purchase(body)
                    elif self.command == 'POST' and path.path == '/tag':
                        result = account.tag(body)
                    elif self.command == 'GET' and path.path == '/describe':
                        result = account.describe({k: v[0] for k, v in parse_qs(path.query).items()})
                    else:
                        raise ValueError('unsupported endpoint')
                    status = 200
                except Exception as exc:
                    status = 400
                    result = {'error': str(exc)}
                data = json.dumps(result).encode()
                try:
                    self.send_response(status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            do_GET = dispatch
            do_POST = dispatch

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return f'http://127.0.0.1:{self.server.server_port}'

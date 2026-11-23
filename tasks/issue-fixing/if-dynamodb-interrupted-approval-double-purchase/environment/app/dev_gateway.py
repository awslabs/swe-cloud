"""Unfaulted local account gateway. Start separately from purchase workers."""
import argparse
import json
import threading
import time
import uuid
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from storage import Store


class Account:
    def __init__(self):
        self.table = Store().db.Table('development_account')
        try:
            self.table.load()
        except Exception:
            Store().db.create_table(TableName=self.table.name,
                                   KeySchema=[{'AttributeName': 'id', 'KeyType': 'HASH'}],
                                   AttributeDefinitions=[{'AttributeName': 'id', 'AttributeType': 'S'}],
                                   BillingMode='PAY_PER_REQUEST')
            self.table.wait_until_exists()
        self.lock = threading.Lock()

    def all(self):
        result = []
        args = {'ConsistentRead': True}
        while True:
            page = self.table.scan(**args)
            result += page.get('Items', [])
            if not page.get('LastEvaluatedKey'):
                return result
            args['ExclusiveStartKey'] = page['LastEvaluatedKey']

    def call(self, method, path, body):
        processing_deadline = time.monotonic() + 15
        with self.lock:
            parsed = urlparse(path)
            if method == 'GET' and parsed.path == '/describe':
                query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
                if query['provider_type'] not in ('token', 'tag_after'):
                    raise ValueError('describe unsupported')
                return [{key: row[key] for key in ('commitment_id', 'offering_id', 'tags', 'created_at')}
                        for row in self.all() if row['provider_type'] == query['provider_type']
                        and row['tags'].get(query['tag_key']) == query['tag_value']]
            if method == 'POST' and parsed.path == '/tag':
                row = self.table.get_item(Key={'id': body['commitment_id']}, ConsistentRead=True)['Item']
                if row['provider_type'] != 'tag_after':
                    raise ValueError('tag unsupported')
                row['tags'].update(body['tags'])
                self.table.put_item(Item=row)
                return {}
            if method != 'POST' or parsed.path != '/purchase':
                raise ValueError('unknown endpoint')
            kind = body['provider_type']
            if kind not in ('token', 'tag_after', 'none') or int(body['count']) <= 0:
                raise ValueError('invalid purchase')
            if kind != 'token' and ('client_token' in body or 'tags' in body):
                raise ValueError('unsupported purchase fields')
            now = int(time.time() * 1000)
            token = body.get('client_token', '')
            if kind == 'token' and token:
                for row in self.all():
                    if row.get('client_token') == token and now - row['created_at'] < 20000:
                        return {'commitment_id': row['commitment_id']}
            cid = str(uuid.uuid4())
            if time.monotonic() >= processing_deadline:
                raise ValueError('purchase processing limit exceeded; request discarded')
            self.table.put_item(Item=dict(id=cid, commitment_id=cid, provider_type=kind,
                                         offering_id=body['offering_id'], count=body['count'],
                                         client_token=token, tags=body.get('tags', {}), created_at=now))
            return {'commitment_id': cid}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8080)
    args = parser.parse_args()
    account = Account()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def dispatch(self):
            try:
                length = int(self.headers.get('Content-Length', 0))
                body = json.loads(self.rfile.read(length)) if length else None
                result = account.call(self.command, self.path, body)
                payload = json.dumps(result, default=lambda value: int(value) if isinstance(value, Decimal) else value).encode()
                self.send_response(200)
            except Exception as exc:
                payload = json.dumps({'error': str(exc)}).encode()
                self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = dispatch
        do_POST = dispatch

    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()


if __name__ == '__main__':
    main()

"""Development deployment of the service's DynamoDB egress gateway.

An upstream request can have completed even when the caller receives a gateway
error. The fault controls reproduce a short outage; there is no application-state
inspection or knowledge of execution identities in this component.
"""
import argparse
import http.server
import json
import threading
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', default='http://localhost:4566')
    parser.add_argument('--port', type=int, default=4567)
    parser.add_argument('--table', default='idempotency')
    parser.add_argument('--failures', type=int, default=0)
    parser.add_argument('--skip-writes', type=int, default=0)
    parser.add_argument('--phase', choices=['before', 'after'], default='before')
    args = parser.parse_args()
    lock = threading.Lock()
    writes_seen = 0

    class Gateway(http.server.BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            nonlocal writes_seen
            body = self.rfile.read(int(self.headers['Content-Length']))
            request = json.loads(body)
            target = self.headers.get('X-Amz-Target', '').split('.')[-1]
            tables = []
            if target in ('PutItem', 'UpdateItem', 'DeleteItem'):
                tables = [request.get('TableName')]
            elif target == 'TransactWriteItems':
                tables = [part[kind].get('TableName') for part in request.get('TransactItems', [])
                          for kind in ('Put', 'Update', 'Delete') if kind in part]
            elif target == 'BatchWriteItem':
                tables = list(request.get('RequestItems', {}))
            fault = False
            with lock:
                if args.table in tables:
                    writes_seen += 1
                    fault = args.skip_writes < writes_seen <= args.skip_writes + args.failures
            try:
                if not fault or args.phase == 'after':
                    headers = {k: v for k, v in self.headers.items()
                               if k.lower() not in ('host', 'content-length', 'connection')}
                    upstream_request = urllib.request.Request(args.upstream, data=body, headers=headers, method='POST')
                    try:
                        response = urllib.request.urlopen(upstream_request, timeout=10)
                    except urllib.error.HTTPError as exc:
                        response = exc
                    with response:
                        status, data = response.status, response.read()
                if fault:
                    status = 500
                    data = b'{"__type":"InternalServerError","message":"upstream outcome unavailable"}'
                print(json.dumps({'request': target, 'gateway_error': fault,
                                  'forwarded': not fault or args.phase == 'after'}), flush=True)
                self.send_response(status)
                self.send_header('Content-Type', 'application/x-amz-json-1.0')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (OSError, urllib.error.URLError):
                self.close_connection = True

    server = http.server.ThreadingHTTPServer(('127.0.0.1', args.port), Gateway)
    print(json.dumps({'listening': args.port, 'upstream': args.upstream}), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()

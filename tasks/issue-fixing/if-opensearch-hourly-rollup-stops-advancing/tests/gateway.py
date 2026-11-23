"""Metered protocol boundary, owned by the verifier."""
import json
import os
import signal
import threading
import urllib.parse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BODY_KEYS = set('query size from aggs aggregations sort search_after _source track_total_hits seq_no_primary_term version stored_fields docvalue_fields'.split())
PARAMS = {'preference', 'routing', 'typed_keys', 'request_cache'}
QUERY_TYPES = {'bool', 'range', 'term', 'terms', 'exists', 'ids', 'match_all', 'match_none', 'constant_score'}
AGG_TYPES = set('composite terms date_histogram histogram range date_range filter filters sum value_count avg min max stats'.split())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def no_scripts(node):
    if isinstance(node, dict):
        require('script' not in node, 'scripts unavailable')
        for value in node.values():
            no_scripts(value)
    elif isinstance(node, list):
        for value in node:
            no_scripts(value)


def query(node):
    require(isinstance(node, dict) and len(node) == 1, 'invalid query clause')
    kind, value = next(iter(node.items()))
    require(kind in QUERY_TYPES and isinstance(value, dict), 'unsupported query')
    if kind == 'bool':
        require(set(value) <= {'must', 'filter', 'should', 'must_not', 'minimum_should_match', 'boost', 'adjust_pure_negative'}, 'bool options')
        for name in ('must', 'filter', 'should', 'must_not'):
            clauses = value.get(name, [])
            for clause in clauses if isinstance(clauses, list) else [clauses]:
                query(clause)
    elif kind == 'constant_score':
        require(set(value) <= {'filter', 'boost'} and 'filter' in value, 'constant_score options')
        query(value['filter'])
    elif kind == 'terms':
        for field, values in value.items():
            if field not in ('boost', '_name'):
                require(isinstance(values, list), 'inline terms required')


def aggregations(node):
    require(isinstance(node, dict), 'invalid aggregations')
    for name, spec in node.items():
        require(isinstance(spec, dict), 'invalid aggregation')
        types = set(spec) - {'aggs', 'aggregations', 'meta'}
        require(len(types) == 1 and types <= AGG_TYPES, 'unsupported aggregation')
        kind = next(iter(types))
        if kind == 'composite':
            sources = spec[kind].get('sources', [])
            for source in sources:
                require(isinstance(source, dict) and len(source) == 1, 'invalid composite source')
                value = next(iter(source.values()))
                require(set(value) <= {'terms', 'date_histogram', 'histogram'} and len(value) == 1,
                        'unsupported composite source')
        elif kind == 'filter':
            query(spec[kind])
        elif kind == 'filters':
            filters = spec[kind]['filters']
            for value in filters.values() if isinstance(filters, dict) else filters:
                query(value)
        for key in ('aggs', 'aggregations'):
            if key in spec:
                aggregations(spec[key])


def validate(body, params):
    require(isinstance(body, dict) and set(body) <= BODY_KEYS, 'unsupported search body')
    require(set(params) <= PARAMS, 'unsupported query parameter')
    no_scripts(body)
    if 'query' in body:
        query(body['query'])
    for key in ('aggs', 'aggregations'):
        if key in body:
            aggregations(body[key])


class Gateway:
    def __init__(self, engine, source, target, meta, transform):
        self.engine, self.source, self.target = engine, source, target
        self.meta, self.transform = meta, transform
        self.docs = 0
        self.credit = 8
        self.accepted = []
        self.shards = 0
        self.log = []
        self.errors = []
        self.inject = None
        self.kill_write = False
        self.killed = False
        self.process = None
        self.process_ready = threading.Event()
        self.on_kill = None
        self.hold_kind = None
        self.held = threading.Event()
        self.release = threading.Event()
        self.held_ids = []
        self.deferred_done = threading.Event()
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                outer.handle(self)

            do_POST = do_GET
            do_PUT = do_GET
            do_DELETE = do_GET

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)

    def reset(self):
        self.docs, self.shards = 0, 0
        self.credit = 8
        self.accepted = []
        self.log, self.errors = [], []
        self.inject = None
        self.kill_write = self.killed = False
        self.process = self.on_kill = None
        self.process_ready.clear()

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def handle(self, handler):
        with self.lock:
            try:
                self._handle(handler)
            finally:
                if self.held.is_set() and self.release.is_set():
                    self.deferred_done.set()

    def _handle(self, handler):
        split = urllib.parse.urlsplit(handler.path)
        path = urllib.parse.unquote(split.path).strip('/')
        params = urllib.parse.parse_qs(split.query, keep_blank_values=True)
        parts = path.split('/')
        method = handler.command
        raw = handler.rfile.read(int(handler.headers.get('Content-Length', 0)))
        record = {'method': method, 'path': path, 'params': params}
        self.log.append(record)
        status, response = 200, None
        try:
            body = json.loads(raw) if raw and path != '_bulk' else None
            search = len(parts) == 2 and parts[1] in ('_search', '_count')
            source_search = search and parts[0] == self.source
            target_write = False
            if search:
                require(method == 'POST' and parts[0] in (self.source, self.target, self.meta), 'search endpoint')
                validate(body or {}, params)
                if parts[1] == '_search':
                    body = {**(body or {}), 'track_total_hits': True}
                raw = json.dumps(body or {}).encode()
            elif len(parts) == 2 and parts == [self.source, '_stats']:
                require(method == 'GET' and set(params) <= {'level'}, 'stats endpoint')
            elif len(parts) == 3 and parts[1] == '_doc':
                options = set() if method == 'GET' else (
                    {'if_seq_no', 'if_primary_term'} if parts[0] == self.meta else
                    {'if_seq_no', 'if_primary_term', 'version', 'version_type'})
                require(set(params) <= options, 'document parameters')
                require(parts[0] == self.target or
                        (parts[0] == self.meta and parts[2] == self.transform), 'document scope')
                require(method in ('GET', 'PUT', 'POST'), 'document method')
                if method != 'GET':
                    no_scripts(body)
                target_write = method != 'GET' and parts[0] == self.target
            elif path == '_bulk':
                require(method == 'POST' and not params, 'bulk endpoint')
                lines = [json.loads(line) for line in raw.splitlines() if line.strip()]
                no_scripts(lines)
                require(len(lines) % 2 == 0, 'bulk action pairs')
                for i in range(0, len(lines), 2):
                    action = lines[i]
                    require(len(action) == 1, 'bulk action')
                    kind, spec = next(iter(action.items()))
                    require(kind in ('index', 'create', 'update'), 'bulk kind')
                    index = spec.get('_index')
                    require(index == self.target or (index == self.meta and spec.get('_id') == self.transform), 'bulk scope')
                    target_write |= index == self.target
            elif len(parts) == 2 and parts[1] == '_refresh':
                require(method == 'POST' and not params and parts[0] in (self.target, self.meta), 'refresh endpoint')
            else:
                raise ValueError('endpoint unavailable')
            meta_write = (len(parts) == 3 and parts[0] == self.meta and method != 'GET')
            if path == '_bulk':
                meta_write = any(next(iter(lines[i].values())).get('_index') == self.meta
                                 for i in range(0, len(lines), 2))
            hold = ((self.hold_kind == 'target' and target_write) or
                    (self.hold_kind == 'metadata' and meta_write and self.accepted))
            if hold and not self.held.is_set():
                if path == '_bulk':
                    self.held_ids = [next(iter(lines[i].values())).get('_id')
                                    for i in range(0, len(lines), 2)
                                    if next(iter(lines[i].values())).get('_index') == self.target]
                elif target_write:
                    self.held_ids = [parts[2]]
                record['deferred_until_replacement'] = True
                self.held.set()
                require(self.release.wait(timeout=240), 'environment: abandoned request never released')
            url = self.engine.url + split.path
            if split.query:
                url += '?' + split.query
            rejection = {'type': 'es_rejected_execution_exception',
                         'reason': 'target allocation exhausted for this invocation'}
            admitted = []
            if path == '_bulk':
                output = [None] * (len(lines) // 2)
                forwarded = []
                positions = []
                for i in range(0, len(lines), 2):
                    kind, spec = next(iter(lines[i].items()))
                    target = spec['_index'] == self.target
                    if target and self.credit <= 0:
                        output[i // 2] = {kind: {'_index': spec['_index'], '_id': spec.get('_id'),
                                                'status': 429, 'error': rejection}}
                    else:
                        if target:
                            self.credit -= 1
                            admitted.append(spec.get('_id'))
                        positions.append(i // 2)
                        forwarded.extend(lines[i:i + 2])
                if forwarded:
                    bulk_raw = ('\n'.join(json.dumps(x) for x in forwarded) + '\n').encode()
                    actual = self.engine.call('POST', '_bulk', bulk_raw)
                    require(len(actual['items']) == len(positions), 'environment: incomplete bulk')
                    for pos, item in zip(positions, actual['items']):
                        output[pos] = item
                response = {'took': 0, 'errors': any(next(iter(x.values()))['status'] >= 300
                                                    for x in output), 'items': output}
            else:
                if target_write:
                    if self.credit <= 0:
                        record['status'] = 429
                        self.reply(handler, 429, {'error': rejection, 'status': 429})
                        return
                    self.credit -= 1
                    admitted.append(parts[2])
                req = urllib.request.Request(url, raw or None, method=method,
                                             headers={'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=90) as forwarded:
                    response = json.load(forwarded)
            if path == '_bulk':
                record['item_statuses'] = [next(iter(item.values()))['status']
                                           for item in response['items']]
            self.accepted.extend(admitted)
            record['target_admitted'] = admitted
            if self.kill_write and admitted and not self.killed:
                # Effects reached the engine, but their acknowledgement is lost with the process.
                self.killed = True
                if self.on_kill:
                    self.on_kill()
                require(self.process_ready.wait(timeout=5), 'missing invocation process')
                os.killpg(self.process.pid, signal.SIGKILL)
                record['status'] = 'interrupted'
                self.reply(handler, 503, {'error': 'worker interrupted'})
                return
            record['forwarded'] = True
            if search:
                require(not response.get('timed_out') and response['_shards'].get('failed', 0) == 0,
                        'partial search response')
                if parts[1] == '_search':
                    total = response['hits']['total']
                    require(total['relation'] == 'eq', 'inexact total')
                    charge = total['value']
                else:
                    charge = response['count']
                self.docs += charge
                record['charge'] = charge
                if source_search:
                    self.shards += response['_shards']['total']
                    if self.inject:
                        self.inject()
        except ValueError as exc:
            status, response = 400, {'error': str(exc)}
        except urllib.error.HTTPError as exc:
            status = exc.code
            response = {'engine_error': exc.read().decode(errors='replace')}
        except Exception as exc:
            status, response = 502, {'gateway_error': repr(exc)}
            self.errors.append(repr(exc))
        record['status'] = status
        self.reply(handler, status, response)

    @staticmethod
    def reply(handler, status, body):
        data = json.dumps(body).encode()
        try:
            handler.send_response(status)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('Content-Length', str(len(data)))
            handler.end_headers()
            handler.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

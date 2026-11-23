"""Verifier-owned Kafka network fault gateway; broker state remains authoritative."""
import json
import socket
import socketserver
import threading
import time
from broker_wire import Reader, frame, blob, i16, i32, i64, string, decode_batches


class Gateway:
    def __init__(self, bootstrap):
        self.bootstrap = bootstrap
        self.lock = threading.Lock()
        self.faults = {}
        self.active = []
        self.events = []
        self.errors = []
        gateway = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self):
                self.request.settimeout(90)
                try:
                    while True:
                        req = frame(self.request)
                        result = gateway.handle(req)
                        if result is None: return
                        self.request.sendall(blob(result))
                except (OSError, ConnectionError): pass
                except Exception as e:
                    with gateway.lock: gateway.errors.append(repr(e))

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.server = Server(('127.0.0.1', 0), Handler)
        self.address = '127.0.0.1:%s' % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def arm(self, key, partition, mode, code, hold=2.6, sticky=8, late=2.3):
        with self.lock:
            self.faults[(partition, key)] = dict(mode=mode, code=code, hold=hold,
                sticky=sticky, late=late)

    def forward(self, request):
        host, port = self.bootstrap.rsplit(':', 1)
        with socket.create_connection((host, int(port)), 30) as s:
            s.settimeout(30); s.sendall(blob(request)); return frame(s)

    def add(self, event):
        with self.lock: self.events.append(event)

    def handle(self, request):
        now = time.monotonic()
        r = Reader(request)
        api, version, corr, client = r.h(), r.h(), r.i(), r.s()
        if api in (24, 25, 26, 28):
            self.add(dict(api=api, time=now, violation='transaction API'))
            return None
        if api == 18:  # advertise only codec versions supported by this deployment
            versions = [(0, 3, 8), (1, 4, 4), (3, 1, 1), (18, 0, 0), (22, 0, 0)]
            return i32(corr) + i16(0) + i32(len(versions)) + b''.join(i16(a)+i16(lo)+i16(hi) for a,lo,hi in versions)
        if api == 22:
            transactional_id = r.s()
            self.add(dict(api=api, time=now, transactional_id=transactional_id))
            if transactional_id is not None:
                return i32(corr) + i32(0) + i16(53) + i64(-1) + i16(-1)
            return self.forward(request)
        if api == 3:
            if version != 1: raise ValueError('gateway Metadata version must be 1')
            response = Reader(self.forward(request)); response.i()
            brokers = response.i()
            rewritten = []
            host, port = self.address.split(':')
            for _ in range(brokers):
                node_id = response.i(); response.s(); response.i(); rack = response.s()
                rewritten.append(i32(node_id) + string(host) + i32(int(port)) + string(rack))
            return i32(corr) + i32(brokers) + b''.join(rewritten) + response.data[response.pos:]
        if api != 0: return self.forward(request)
        if version < 3 or version > 8: raise ValueError('Produce version outside deployment range')
        txn, acks, timeout = r.s(), r.h(), r.i()
        topics = []
        for _ in range(r.i()):
            topic = r.s(); partitions = []
            for _ in range(r.i()):
                partition, data = r.i(), r.b()
                partitions.append((partition, data))
            topics.append((topic, partitions))
        responses = []
        for topic, partitions in topics:
            parts = []
            for partition, data in partitions:
                records = decode_batches(data)
                if not records: raise ValueError('empty Produce batch')
                first = records[0]
                identity = (partition, first['pid'], first['epoch'], first['seq'], records[-1]['seq'])
                keys = [x['key'] for x in records]
                event = dict(api=0, time=now, partition=partition, pid=first['pid'],
                    epoch=first['epoch'], seq=first['seq'], last_seq=records[-1]['seq'],
                    keys=keys, attrs=first['attrs'], acks=acks, transactional_id=txn,
                    forwarded=None, broker_code=None, broker_offset=None, returned_code=None)
                gates = []
                with self.lock:
                    spec = None
                    for key in keys:
                        spec = self.faults.pop((partition, key), None)
                        if spec is not None: break
                    if spec is not None:
                        spec = dict(spec, identity=identity, keys=keys, start=now, gate=threading.Event())
                        self.active.append(spec)
                        initial = True
                    else:
                        initial = False
                        for previous in self.active:
                            if (previous['identity'] == identity and previous['keys'] == keys
                                and now < previous['start'] + previous['sticky']):
                                spec = previous; break
                    for previous in self.active:
                        if (previous is not spec and previous['mode'] == 'late'
                            and previous['identity'][:3] == identity[:3]
                            and not previous['gate'].is_set()): gates.append(previous['gate'])
                    self.events.append(event)
                for gate in gates:
                    if not gate.wait(30): raise TimeoutError('late-forward barrier')
                should_forward = spec is None or spec['mode'] != 'drop'
                event['mode'] = spec['mode'] if spec else None
                if spec and initial and spec['mode'] == 'late':
                    time.sleep(max(0, spec['start'] + spec['late'] - time.monotonic()))
                if should_forward:
                    single = string(txn) + i16(acks) + i32(timeout) + i32(1) + string(topic) + i32(1) + i32(partition) + blob(data)
                    req = i16(0) + i16(version) + i32(corr) + string(client) + single
                    event['forwarded'] = time.monotonic()
                    answer = Reader(self.forward(req)); answer.i(); answer.i(); answer.s(); answer.i(); answer.i()
                    code, offset = answer.h(), answer.q()
                    event['broker_code'], event['broker_offset'] = code, offset
                else: code, offset = 7, -1
                if spec and initial: spec['gate'].set()
                if spec:
                    time.sleep(max(0, now + spec['hold'] - time.monotonic()))
                    code, offset = (spec['code'] if initial else 7), -1
                event['returned_code'] = code
                event['returned'] = time.monotonic()
                part = i32(partition) + i16(code) + i64(offset) + i64(-1)
                if version >= 5: part += i64(-1)
                if version >= 8: part += i32(0) + string(None)
                parts.append(part)
            responses.append(string(topic) + i32(len(parts)) + b''.join(parts))
        return i32(corr) + i32(len(responses)) + b''.join(responses) + i32(0)

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.events))

    def close(self):
        self.server.shutdown(); self.server.server_close()

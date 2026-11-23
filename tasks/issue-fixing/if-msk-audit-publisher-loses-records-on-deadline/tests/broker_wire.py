"""Explicit Kafka wire operations. No retries or producer-state management.

Each RPC uses its own connection: callers choose the identity and sequence.
Produce uses v3, acks=all, one uncompressed v2 record batch.
"""
import socket
import struct
import time
import google_crc32c


def i16(n): return struct.pack('>h', n)
def i32(n): return struct.pack('>i', n)
def i64(n): return struct.pack('>q', n)
def string(s):
    if s is None: return i16(-1)
    b = s.encode(); return i16(len(b)) + b

def blob(b): return i32(len(b)) + b


class Reader:
    def __init__(self, data): self.data, self.pos = data, 0
    def take(self, n):
        if n < 0 or self.pos + n > len(self.data): raise ValueError('truncated Kafka data')
        out = self.data[self.pos:self.pos+n]; self.pos += n; return out
    def num(self, fmt): return struct.unpack('>' + fmt, self.take(struct.calcsize('>' + fmt)))[0]
    def h(self): return self.num('h')
    def i(self): return self.num('i')
    def q(self): return self.num('q')
    def s(self):
        n = self.h(); return None if n < 0 else self.take(n).decode()
    def b(self):
        n = self.i(); return None if n < 0 else self.take(n)


def exact(sock, n):
    out = bytearray()
    while len(out) < n:
        b = sock.recv(n - len(out))
        if not b: raise ConnectionError('Kafka connection closed')
        out.extend(b)
    return bytes(out)


def frame(sock):
    n = struct.unpack('>i', exact(sock, 4))[0]
    if n < 0 or n > 32 * 1024 * 1024: raise ValueError('invalid frame size')
    return exact(sock, n)


def rpc(bootstrap, api, version, body, timeout=10):
    host, port = bootstrap.rsplit(':', 1)
    with socket.create_connection((host, int(port)), timeout=max(.001, timeout)) as s:
        s.settimeout(max(.001, timeout))
        request = i16(api) + i16(version) + i32(1) + string('audit-publisher') + body
        s.sendall(blob(request))
        answer = Reader(frame(s))
        if answer.i() != 1: raise ValueError('correlation mismatch')
        return answer


def varint(n):
    n = (n << 1) ^ (n >> 63); out = bytearray()
    while n > 127: out.append((n & 127) | 128); n >>= 7
    out.append(n); return bytes(out)


def readvar(r):
    n = shift = 0
    while True:
        b = r.num('B'); n |= (b & 127) << shift
        if not b & 128: return (n >> 1) ^ -(n & 1)
        shift += 7
        if shift > 70: raise ValueError('invalid varint')


def batch(pid, epoch, seq, records):
    encoded = bytearray()
    for j, (key, value) in enumerate(records):
        key, value = key.encode(), value.encode()
        rec = b'\0' + varint(0) + varint(j) + varint(len(key)) + key + varint(len(value)) + value + varint(0)
        encoded.extend(varint(len(rec)) + rec)
    now = int(time.time() * 1000)
    tail = i16(0) + i32(len(records)-1) + i64(now) + i64(now) + i64(pid) + i16(epoch) + i32(seq) + i32(len(records)) + encoded
    crc = struct.pack('>I', google_crc32c.value(bytes(tail)))
    content = i32(-1) + b'\x02' + crc + tail
    return i64(0) + i32(len(content)) + content


def decode_batches(data):
    r = Reader(data); out = []
    while len(data) - r.pos >= 12:
        base, size = r.q(), r.i()
        if size > len(data) - r.pos: break  # Fetch may end in a partial batch.
        b = Reader(r.take(size)); b.i()
        if b.num('B') != 2: raise ValueError('expected record batch v2')
        b.i(); attrs = b.h(); b.i(); b.q(); b.q()
        pid, epoch, seq, count = b.q(), b.h(), b.i(), b.i()
        if attrs & 7: raise ValueError('compressed batch unsupported')
        for _ in range(count):
            rec = Reader(b.take(readvar(b))); rec.num('B'); readvar(rec)
            delta = readvar(rec); n = readvar(rec)
            key = rec.take(n).decode() if n >= 0 else None
            n = readvar(rec); value = rec.take(n).decode() if n >= 0 else None
            out.append(dict(offset=base+delta, key=key, value=value, pid=pid,
                            epoch=epoch, seq=seq+delta, attrs=attrs))
    return out


def init_producer_id(bootstrap, timeout=10):
    r = rpc(bootstrap, 22, 0, string(None) + i32(60000), timeout)
    r.i(); code = r.h(); pid, epoch = r.q(), r.h()
    if code: raise RuntimeError('InitProducerId error %s' % code)
    return pid, epoch


def produce(bootstrap, topic, partition, pid, epoch, seq, records, timeout=10):
    data = batch(pid, epoch, seq, records)
    body = string(None) + i16(-1) + i32(10000) + i32(1) + string(topic) + i32(1) + i32(partition) + blob(data)
    r = rpc(bootstrap, 0, 3, body, timeout)
    if r.i() != 1 or r.s() != topic or r.i() != 1 or r.i() != partition:
        raise ValueError('unexpected produce response')
    return r.h(), r.q()


def create_topic(bootstrap, topic, partitions=2):
    body = i32(1) + string(topic) + i32(partitions) + i16(1) + i32(0) + i32(0) + i32(30000)
    r = rpc(bootstrap, 19, 0, body, 40)
    if r.i() != 1 or r.s() != topic: raise ValueError('unexpected create response')
    code = r.h()
    if code not in (0, 36): raise RuntimeError('CreateTopics error %s' % code)


def fetch(bootstrap, topic, partition, offset=0):
    body = i32(-1) + i32(100) + i32(1) + i32(8*1024*1024) + b'\0' + i32(1) + string(topic) + i32(1) + i32(partition) + i64(offset) + i32(8*1024*1024)
    r = rpc(bootstrap, 1, 4, body)
    r.i()
    if r.i() != 1 or r.s() != topic or r.i() != 1 or r.i() != partition:
        raise ValueError('unexpected fetch response')
    code, high = r.h(), r.q(); r.q()
    aborted = r.i()
    for _ in range(max(0, aborted)): r.q(); r.q()
    data = r.b() or b''
    if code: raise RuntimeError('Fetch error %s' % code)
    return high, decode_batches(data)

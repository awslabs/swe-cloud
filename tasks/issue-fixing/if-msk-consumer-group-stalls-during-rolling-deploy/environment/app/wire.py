"""Classic Kafka wire primitives used by the group controller.

Versions are deliberately non-flexible: the MSK endpoint serves JoinGroup <= 6.
Each RPC has its own connection so a blocking JoinGroup doesn't block admin reads.
"""
import socket
import struct
from collections import defaultdict


def i(n): return struct.pack('>i', n)
def h(n): return struct.pack('>h', n)
def q(n): return struct.pack('>q', n)
def s(value):
    if value is None: return h(-1)
    value = value.encode()
    return h(len(value)) + value

def b(value): return i(len(value)) + value
def a(values): return i(len(values)) + b''.join(values)


class Reader:
    def __init__(self, data): self.data, self.pos = data, 0
    def take(self, n):
        result = self.data[self.pos:self.pos+n]
        self.pos += n
        if len(result) != n: raise ValueError('truncated Kafka response')
        return result
    def num(self, fmt): return struct.unpack(fmt, self.take(struct.calcsize(fmt)))[0]
    def i(self): return self.num('>i')
    def h(self): return self.num('>h')
    def q(self): return self.num('>q')
    def s(self):
        n = self.h()
        return None if n < 0 else self.take(n).decode()
    def b(self):
        n = self.i()
        return b'' if n < 0 else self.take(n)
    def array(self, fn): return [fn() for _ in range(max(0, self.i()))]


def exact(sock, n):
    value = b''
    while len(value) < n:
        piece = sock.recv(n-len(value))
        if not piece: raise OSError('Kafka connection closed')
        value += piece
    return value


class Broker:
    def __init__(self, bootstrap): self.address = bootstrap.split(',')[0]
    def rpc(self, key, version, body, timeout=40):
        host, port = self.address.rsplit(':', 1)
        request = h(key) + h(version) + i(1) + s('orders-worker') + body
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.sendall(b(request))
            response = Reader(exact(sock, struct.unpack('>i', exact(sock, 4))[0]))
        response.i()
        return response

    def coordinator(self, group):
        r = self.rpc(10, 1, s(group) + b'\x00')
        r.i(); error = r.h(); r.s(); r.i(); host = r.s(); port = r.i()
        check(error)
        other = Broker(f'{host}:{port}')
        return other

    def describe(self, group):
        r = self.rpc(15, 4, a([s(group)]) + b'\x00')
        r.i()
        groups = []
        for _ in range(r.i()):
            error, name, state, kind, protocol = r.h(), r.s(), r.s(), r.s(), r.s()
            members = []
            for _ in range(r.i()):
                mid, instance, client, host = r.s(), r.s(), r.s(), r.s()
                metadata, assignment = r.b(), r.b()
                members.append(dict(id=mid, instance=instance, client=client, host=host,
                                    metadata=metadata, assignment=assignment))
            r.i()
            check(error)
            groups.append(dict(name=name, state=state, kind=kind, protocol=protocol, members=members))
        return groups[0]

    def leave(self, group, instance, member=''):
        r = self.rpc(13, 3, s(group) + a([s(member)+s(instance)]))
        r.i(); check(r.h())
        for _ in range(r.i()):
            r.s(); r.s(); check(r.h())

    def offsets(self, group, topic, partitions):
        r = self.rpc(9, 1, s(group)+a([s(topic)+a([i(p) for p in partitions])]))
        result = {}
        for _ in range(r.i()):
            topic_name = r.s()
            for _ in range(r.i()):
                part, offset, metadata, error = r.i(), r.q(), r.s(), r.h()
                check(error)
                result[(topic_name, part)] = offset
        return result

    def commit(self, group, generation, member, offsets):
        topics = defaultdict(list)
        for (topic, part), offset in offsets.items():
            topics[topic].append(i(part)+q(offset)+s(''))
        if not topics: return
        body = s(group)+i(generation)+s(member)+q(-1)
        body += a([s(topic)+a(parts) for topic, parts in topics.items()])
        r = self.rpc(8, 2, body)
        for _ in range(r.i()):
            r.s()
            for _ in range(r.i()):
                r.i(); check(r.h())


class KafkaError(Exception):
    def __init__(self, code): self.code = code; super().__init__(f'Kafka error {code}')


def check(code):
    if code: raise KafkaError(code)


def partition_map(r):
    result = set()
    for _ in range(r.i()):
        topic = r.s()
        for _ in range(r.i()): result.add((topic, r.i()))
    return result


def encode_partitions(partitions):
    topics = defaultdict(list)
    for topic, part in sorted(partitions): topics[topic].append(i(part))
    return a([s(topic)+a(parts) for topic, parts in topics.items()])


def subscription(topic, owned, user_data=b''):
    return h(1)+a([s(topic)])+b(user_data)+encode_partitions(owned)


def read_subscription(data):
    r = Reader(data)
    version = r.h()
    topics = r.array(r.s)
    r.b()
    owned = partition_map(r) if version >= 1 else set()
    return topics, owned


def assignment(partitions): return h(0)+encode_partitions(partitions)+b(b'')


def read_assignment(data):
    if not data: return set()
    r = Reader(data); r.h()
    return partition_map(r)

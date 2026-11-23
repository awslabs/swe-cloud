"""Independent Kafka non-flexible group protocol codec used by the traffic observer."""
import struct


def num(fmt, n):
    return struct.pack('>' + fmt, n)


def string(s):
    return num('h', -1) if s is None else num('h', len(s.encode())) + s.encode()


def blob(b):
    return num('i', len(b)) + b


def array(xs):
    return num('i', len(xs)) + b''.join(xs)


def uv(n):
    out = b''
    while n > 127:
        out += bytes([(n & 127) | 128])
        n >>= 7
    return out + bytes([n])


class Reader:
    def __init__(self, b):
        self.b, self.i = b, 0

    def n(self, fmt):
        n = struct.calcsize('>' + fmt)
        result = struct.unpack_from('>' + fmt, self.b, self.i)[0]
        self.i += n
        return result

    def raw(self, n):
        b = self.b[self.i:self.i+n]
        self.i += n
        return b

    def s(self):
        n = self.n('h')
        return None if n < 0 else self.raw(n).decode()

    def bytes(self):
        n = self.n('i')
        return b'' if n < 0 else self.raw(n)

    def uv(self):
        out = shift = 0
        while True:
            n = self.n('B')
            out |= (n & 127) << shift
            if not n & 128:
                return out
            shift += 7

    def tags(self):
        for _ in range(self.uv()):
            self.uv()
            self.raw(self.uv())


def assignment(b):
    r = Reader(b)
    if not b:
        return {}
    r.n('h')
    topics = {}
    for _ in range(r.n('i')):
        t = r.s()
        topics[t] = [r.n('i') for _ in range(r.n('i'))]
    return topics


def subscription(b):
    r = Reader(b)
    version = r.n('h')
    for _ in range(r.n('i')):
        r.s()
    r.bytes()
    owned = {}
    if version >= 1:
        for _ in range(r.n('i')):
            t = r.s()
            owned[t] = [r.n('i') for _ in range(r.n('i'))]
    return owned


CAPS = {3: 1, 8: 7, 9: 5, 10: 2, 11: 5, 12: 3, 13: 1, 14: 3, 15: 0, 16: 0}


def request(b):
    r = Reader(b)
    api, ver, corr, client = r.n('h'), r.n('h'), r.n('i'), r.s()
    d = dict(api=api, ver=ver, corr=corr, worker=client)
    if api in (8, 9, 11, 12, 13, 14):
        d['group'] = r.s()
    if api == 11:
        d['session'] = r.n('i')
        if ver >= 1:
            r.n('i')
        d['member'] = r.s()
        d['instance'] = r.s() if ver >= 5 else None
        d['protocol_type'] = r.s()
        protocols = {}
        for _ in range(r.n('i')):
            name = r.s()
            protocols[name] = subscription(r.bytes())
        d['protocols'] = protocols
    elif api in (8, 12, 14):
        d['gen'], d['member'] = r.n('i'), r.s()
        if (api == 8 and ver >= 7) or (api in (12, 14) and ver >= 3):
            d['instance'] = r.s()
        if api == 8:
            if 2 <= ver <= 4:
                r.n('q')
            offsets = {}
            for _ in range(r.n('i')):
                t = r.s()
                for _ in range(r.n('i')):
                    p, o = r.n('i'), r.n('q')
                    if ver >= 6:
                        r.n('i')
                    if ver == 1:
                        r.n('q')
                    r.s()
                    offsets[f'{t}:{p}'] = o
            d['offsets'] = offsets
        elif api == 14:
            plans = {}
            for _ in range(r.n('i')):
                member = r.s()
                plans[member] = assignment(r.bytes())
            d['plans'] = plans
    return d


def response(b, req, port):
    """Decode observed replies; only route rewriting and version capping alter bytes."""
    api, v = req['api'], req['ver']
    r = Reader(b)
    corr = r.n('i')
    d = dict(api=api, worker=req['worker'], corr=corr)
    if api == 18:
        err = r.n('h')
        count = r.uv()-1 if v >= 3 else r.n('i')
        entries = []
        for _ in range(count):
            key, lo, hi = r.n('h'), r.n('h'), r.n('h')
            if v >= 3:
                r.tags()
            hi = min(hi, CAPS.get(key, hi))
            if lo <= hi:
                entries.append(num('h', key)+num('h', lo)+num('h', hi)+(b'\0' if v >= 3 else b''))
        throttle = r.n('i') if v >= 1 else 0
        body = num('i', corr)+num('h', err)
        body += (uv(len(entries)+1)+b''.join(entries)) if v >= 3 else array(entries)
        if v >= 1:
            body += num('i', throttle)
        if v >= 3:
            body += b'\0'
        return body, d
    if api == 3:
        brokers = []
        for _ in range(r.n('i')):
            node, host, oldport = r.n('i'), r.s(), r.n('i')
            rack = r.s() if v >= 1 else None
            brokers.append(num('i', node)+string('127.0.0.1')+num('i', port)+(string(rack) if v >= 1 else b''))
        return num('i', corr)+array(brokers)+b[r.i:], d
    if api == 10:
        # v1/v2 add throttle_time_ms and error_message. Preserve those fields
        # and the requested wire version when changing only the route.
        if v >= 1:
            r.n('i')
        r.n('h')
        if v >= 1:
            r.s()
        r.n('i')
        prefix = b[:r.i]
        r.s()
        r.n('i')
        return prefix+string('127.0.0.1')+num('i', port)+b[r.i:], d
    if api == 11:
        if v >= 2:
            r.n('i')
        d['error'], d['gen'] = r.n('h'), r.n('i')
        d['protocol'], d['leader'], d['member'] = r.s(), r.s(), r.s()
    elif api == 14:
        if v >= 1:
            r.n('i')
        d['error'] = r.n('h')
        d['assignment'] = assignment(r.bytes())
        d['gen'], d['member'] = req['gen'], req['member']
    elif api == 12:
        if v >= 1:
            r.n('i')
        d['error'] = r.n('h')
        d['gen'], d['member'] = req['gen'], req['member']
    elif api in (8, 9):
        if (api == 8 and v >= 3) or (api == 9 and v >= 3):
            r.n('i')
        parts = {}
        for _ in range(r.n('i')):
            t = r.s()
            for _ in range(r.n('i')):
                p = r.n('i')
                part = {}
                if api == 9:
                    part['offset'] = r.n('q')
                    if v >= 5:
                        r.n('i')
                    r.s()
                else:
                    part['offset'] = req['offsets'].get(f'{t}:{p}')
                part['error'] = r.n('h')
                parts[f'{t}:{p}'] = part
        d['parts'] = parts
        if api == 8:
            d['gen'], d['member'] = req['gen'], req['member']
    return b, d

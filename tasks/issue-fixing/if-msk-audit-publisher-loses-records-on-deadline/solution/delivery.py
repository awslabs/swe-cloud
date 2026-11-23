"""Idempotent pipelining with bounded sequence history and generation draining."""
import threading
import time
from collections import deque
from dataclasses import dataclass, field
import kwire

RETRIABLE = {3, 5, 6, 7, 13, 19, 20, 45, 56}
WINDOW = 4


@dataclass
class Batch:
    key: str
    value: str
    deadline: float
    sent: bool = False
    pid: int = -1
    epoch: int = -1
    seq: int = 0
    state: str = 'queued'
    result: object = None
    ready: object = field(default_factory=threading.Event)


class Session:
    def __init__(self, bootstrap):
        self.bootstrap = bootstrap
        self.initial = kwire.init_producer_id(bootstrap)


class DeliveryBuffer:
    def __init__(self, partition, session, topic):
        self.partition, self.session, self.topic = partition, session, topic
        self.changed = threading.Condition()
        self.identity = session.initial
        self.seq = 0
        self.established = False
        self.waiting = deque()
        self.window = deque()
        self.retiring = False
        self.allocating = False

    def finish(self, batch, result):
        batch.state = 'done'
        batch.result = result
        batch.ready.set()

    def fail(self, batch, reason):
        if batch.sent:
            # Stop admissions, but do not change the namespace of any live RPC.
            self.retiring = True
        self.finish(batch, {'status': 'failed', 'error': reason})

    def allocate(self, deadline):
        try:
            identity = kwire.init_producer_id(self.session.bootstrap,
                timeout=max(.001, deadline - time.monotonic()))
        except (OSError, RuntimeError):
            with self.changed:
                self.allocating = False
                # The allocator cannot safely retry a lost allocation reply
                # without another earned allocation. Fail the queued calls.
                for batch in self.waiting:
                    if batch.state != 'done':
                        self.finish(batch, {'status': 'failed', 'error': 'producer allocation failed'})
                self.waiting.clear()
            return
        with self.changed:
            self.identity = identity
            self.allocating = False
            self.drive()

    def drive(self):
        if self.retiring:
            if any(b.state == 'active' for b in self.window):
                return
            # Each blocked successor has received a definite range rejection
            # after all of its older ambiguous attempts. There is no second
            # in-flight attempt for that successor. Only these records may move.
            blocked = [b for b in self.window if b.state == 'blocked']
            for batch in reversed(blocked):
                batch.sent = False
                batch.state = 'queued'
                self.waiting.appendleft(batch)
            self.window.clear()
            self.identity = None
            self.seq = 0
            self.established = False
            self.retiring = False
        # Completed later calls still occupy history behind an unresolved head.
        while self.window and self.window[0].state == 'done':
            self.window.popleft()
        while self.waiting and self.waiting[0].state == 'done':
            self.waiting.popleft()
        if self.identity is None:
            if self.waiting and not self.allocating:
                self.allocating = True
                deadline = min(b.deadline for b in self.waiting if b.state != 'done')
                threading.Thread(target=self.allocate, args=(deadline,), daemon=True).start()
            return
        while self.waiting and len(self.window) < WINDOW:
            # Redpanda accepts an arbitrary initial sequence for an unknown
            # producer. Establish zero before allowing cross-connection races.
            if not self.established and self.window:
                return
            batch = self.waiting.popleft()
            if batch.state == 'done':
                continue
            if time.monotonic() >= batch.deadline:
                self.fail(batch, 'deadline in admission queue')
                continue
            batch.pid, batch.epoch = self.identity
            batch.seq = self.seq
            self.seq += 1
            batch.sent = True
            batch.state = 'active'
            self.window.append(batch)
            threading.Thread(target=self.deliver, args=(batch,), daemon=True).start()

    def deliver(self, batch):
        while True:
            with self.changed:
                if batch.state != 'active':
                    return
                remaining = batch.deadline - time.monotonic()
                if remaining <= 0:
                    self.fail(batch, 'delivery deadline exceeded')
                    self.drive()
                    return
                # One synchronous attempt at a time, always in the first namespace.
                stamp = batch.pid, batch.epoch, batch.seq
            try:
                code, offset = kwire.produce(self.session.bootstrap, self.topic,
                    self.partition, *stamp, [(batch.key, batch.value)], timeout=remaining)
            except (OSError, ConnectionError, TimeoutError):
                code, offset = 7, -1
            with self.changed:
                if batch.state != 'active':
                    return
                if code == 0:
                    self.established = True
                    self.finish(batch, {'status': 'acked', 'partition': self.partition,
                                        'offset': offset})
                    self.drive()
                    return
                if code == 45 and self.retiring:
                    # The bounded horizon ensures that an appended batch would
                    # still be in this producer's dedup cache. It was not stored.
                    batch.state = 'blocked'
                    self.drive()
                    return
                if code not in RETRIABLE:
                    self.fail(batch, 'broker error %s' % code)
                    self.drive()
                    return
            time.sleep(min(.025, max(0, batch.deadline - time.monotonic())))

    def publish(self, key, value, deadline):
        batch = Batch(key, value, deadline)
        with self.changed:
            self.waiting.append(batch)
            self.drive()
        batch.ready.wait(max(0, deadline - time.monotonic()))
        with self.changed:
            if batch.state != 'done':
                self.fail(batch, 'delivery deadline exceeded')
                self.drive()
            return batch.result


class Publisher:
    def __init__(self, bootstrap, topic):
        self.session = Session(bootstrap)
        self.buffers = [DeliveryBuffer(p, self.session, topic) for p in (0, 1)]

    def publish(self, data):
        start = time.monotonic()
        partition = data['partition']
        if type(partition) is not int or partition not in (0, 1):
            raise ValueError('partition must be 0 or 1')
        if not isinstance(data['id'], str) or not isinstance(data['value'], str):
            raise ValueError('id and value must be strings')
        deadline_ms = data['deadline_ms']
        if type(deadline_ms) is not int or deadline_ms <= 0:
            raise ValueError('deadline_ms must be positive')
        return self.buffers[partition].publish(data['id'], data['value'],
            start + deadline_ms / 1000)

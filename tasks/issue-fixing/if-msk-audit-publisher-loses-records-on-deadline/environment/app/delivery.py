"""Concurrent single-record delivery windows and the process producer session."""
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
        self.lock = threading.Lock()
        self.pid, self.epoch = kwire.init_producer_id(bootstrap)

    def stamp(self, buffer, batch):
        with self.lock:
            identity = (self.pid, self.epoch)
            if buffer.identity != identity:
                buffer.identity = identity
                buffer.seq = 0
            if not batch.sent or (batch.pid, batch.epoch) != identity:
                batch.pid, batch.epoch = identity
                batch.seq = buffer.seq
                buffer.seq += 1
            batch.sent = True

    def reload(self):
        with self.lock:
            self.epoch += 1


class DeliveryBuffer:
    def __init__(self, partition, session, topic):
        self.partition, self.session, self.topic = partition, session, topic
        self.changed = threading.Condition()
        self.identity = None
        self.seq = 0
        self.waiting = deque()
        self.window = deque()

    def finish(self, batch, result):
        batch.state = 'done'
        batch.result = result
        batch.ready.set()

    def fail(self, batch, reason):
        self.seq = batch.seq
        self.finish(batch, {'status': 'failed', 'error': reason})

    def drive(self):
        while self.window and self.window[0].state == 'done':
            self.window.popleft()
        while self.waiting and len(self.window) < WINDOW:
            batch = self.waiting.popleft()
            if batch.state == 'done':
                continue
            if time.monotonic() >= batch.deadline:
                self.fail(batch, 'deadline in admission queue')
                continue
            self.session.stamp(self, batch)
            batch.state = 'active'
            self.window.append(batch)
            threading.Thread(target=self.deliver, args=(batch,), daemon=True).start()

    def deliver(self, batch):
        while True:
            with self.changed:
                if batch.state == 'done':
                    return
                remaining = batch.deadline - time.monotonic()
                if remaining <= 0:
                    self.fail(batch, 'delivery deadline exceeded')
                    self.drive()
                    return
                self.session.stamp(self, batch)
                stamp = batch.pid, batch.epoch, batch.seq
            try:
                code, offset = kwire.produce(self.session.bootstrap, self.topic,
                    self.partition, *stamp, [(batch.key, batch.value)], timeout=remaining)
            except (OSError, ConnectionError, TimeoutError):
                code, offset = 7, -1
            with self.changed:
                if batch.state == 'done':
                    return
                if code == 0:
                    self.finish(batch, {'status': 'acked', 'partition': self.partition,
                                        'offset': offset})
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

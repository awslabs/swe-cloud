"""Independent broker observations; no imports from the submitted application."""
import collections
import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import boto3
from botocore.config import Config
from confluent_kafka import Producer, Consumer, TopicPartition
from confluent_kafka.admin import AdminClient, NewTopic
import pytest
from kafka_observer import Broker, KafkaError, read_assignment
from enrichment_service import Enrichment

LOG = Path('/logs/verifier')
# COORDINATOR_LOAD_IN_PROGRESS, COORDINATOR_NOT_AVAILABLE, NOT_COORDINATOR: transient broker states.
RETRIABLE_COORDINATOR = {14, 15, 16}


def wait_for(fn, timeout, message):
    deadline = time.monotonic()+timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last: return last
        time.sleep(0.2)
    raise AssertionError(f'{message}; last observation={last!r}')


@pytest.fixture(scope='session')
def bootstrap():
    try:
        client = boto3.client('kafka', config=Config(read_timeout=600, retries={'max_attempts': 1}))
        arn = client.create_cluster(ClusterName='verify-'+uuid.uuid4().hex[:12], KafkaVersion='3.6.0',
                                   NumberOfBrokerNodes=1,
                                   BrokerNodeGroupInfo={'ClientSubnets': ['subnet-local'],
                                                        'InstanceType': 'kafka.m5.large'})['ClusterArn']
        wait_for(lambda: client.describe_cluster(ClusterArn=arn)['ClusterInfo']['State']=='ACTIVE',
                 60, 'MSK not ACTIVE')
        endpoint = client.get_bootstrap_brokers(ClusterArn=arn)['BootstrapBrokerString']
        admin = AdminClient({'bootstrap.servers': endpoint})
        def broker_ready():
            try: return bool(admin.list_topics(timeout=3).brokers)
            except Exception: return False
        wait_for(broker_ready, 30, 'broker unavailable')
    except Exception as exc:
        (LOG/'environment.txt').write_text(f'environment: MSK preflight failed: {exc!r}\n')
        pytest.fail(f'environment: MSK preflight failed: {exc!r}')
    (LOG/'cluster.json').write_text(json.dumps({'arn': arn, 'bootstrap': endpoint}))
    yield endpoint
    try:
        version = client.describe_cluster(ClusterArn=arn)['ClusterInfo']['CurrentVersion']
        client.delete_cluster(ClusterArn=arn, CurrentVersion=version)
    except Exception: pass


class Barrier:
    def __init__(self, hold, on_call):
        self.on_call = on_call
        self.hold = hold
        self.calls = queue.Queue()
        self.events = []
        outer = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                facts = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                outer.on_call(facts)
                release = threading.Event()
                outer.events.append(release)
                held = outer.hold
                if held: outer.hold = False
                outer.calls.put((facts, release))
                if held: release.wait(20)
                try:
                    self.send_response(200); self.end_headers(); self.wfile.write(b'ok')
                except (BrokenPipeError, ConnectionResetError): pass
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        for e in self.events: e.set()
        self.server.shutdown(); self.server.server_close()


class Scenario:
    def __init__(self, bootstrap, label, count, hold):
        self.bootstrap, self.count = bootstrap, count
        self.label = label+'-'+uuid.uuid4().hex[:8]
        token = uuid.uuid4().hex[:12]
        self.topic, self.aux, self.group = ('orders-'+token, 'aux-'+token, 'ingest-'+token)
        self.output = 'receipts-'+token
        suffix = uuid.uuid4().hex[:5]
        self.ids = {'lead': 'control-'+suffix, 'a': 'reader-a-'+suffix, 'b': 'reader-b-'+suffix}
        self.admin = AdminClient({'bootstrap.servers': bootstrap})
        futures = self.admin.create_topics([NewTopic(self.topic, count, 1), NewTopic(self.aux, 1, 1), NewTopic(self.output, 1, 1)])
        for future in futures.values(): future.result(20)
        self.broker = None
        self.coordinated(lambda broker: None)
        self.violations = []
        self.enrichment = Enrichment(self.violations)
        self.barrier = Barrier(hold, self.enrichment.handoff)
        self.inputs, self.receipts = {}, {}
        self.data_lock = threading.Lock()
        self.audit_armed = False
        self.procs, self.files = [], []
        self.history, self.lock = [], threading.Lock()
        self.done = threading.Event()
        self.observer = threading.Thread(target=self.observe, daemon=True)
        self.observer.start()

    def coordinated(self, call, patience=10):
        """Run a coordinator RPC, re-resolving the coordinator on transient coordinator errors."""
        deadline = time.monotonic()+patience
        while True:
            try:
                broker = self.broker or Broker(self.bootstrap).coordinator(self.group)
                self.broker = broker
                return call(broker)
            except KafkaError as exc:
                if exc.code not in RETRIABLE_COORDINATOR or time.monotonic() > deadline: raise
                self.broker = None
                time.sleep(0.2)

    def view(self):
        raw = self.coordinated(lambda broker: broker.describe(self.group))
        return dict(state=raw['state'], kind=raw['kind'], protocol=raw['protocol'],
                    members={m['instance']: sorted(read_assignment(m['assignment'])) for m in raw['members']})

    def observe(self):
        output = Consumer({'bootstrap.servers': self.bootstrap, 'group.id': 'audit-'+self.label,
                           'enable.auto.commit': False, 'auto.offset.reset': 'earliest'})
        output.assign([TopicPartition(self.output, 0, 0)])
        last = -1
        with (LOG/(self.label+'-coordinator.jsonl')).open('w') as log:
            while not self.done.is_set():
                timestamp = time.monotonic()
                try:
                    snapshot = self.view()
                    # Read the commit first, then consume through the output watermark.
                    # Anything published before that commit must precede this watermark.
                    offsets = self.coordinated(lambda broker: broker.offsets(self.group, self.topic, list(range(self.count))))
                    high = output.get_watermark_offsets(TopicPartition(self.output, 0), timeout=3)[1]
                    deadline = time.monotonic()+5
                    while last < high-1 and time.monotonic()<deadline:
                        record = output.poll(0.1)
                        if record is None: continue
                        if record.error(): raise RuntimeError(record.error())
                        last = record.offset()
                        key, value = self.enrichment.validate(record.value())
                        with self.data_lock: self.receipts[key] = value
                    if last < high-1: raise RuntimeError('receipt auditor could not reach snapshot watermark')
                    if self.audit_armed:
                        with self.data_lock:
                            missing = [key for key, value in self.inputs.items()
                                       if key[2] < offsets.get(key[:2], -1)
                                       and self.receipts.get(key) != value]
                        if missing:
                            self.violations.append(f'checkpoint precedes durable output: {missing[:4]}')
                except Exception as exc: snapshot = dict(error=repr(exc))
                with self.lock: self.history.append((timestamp, snapshot))
                log.write(json.dumps(dict(time=timestamp, **snapshot))+'\n'); log.flush()
                self.done.wait(0.2)
        output.close()

    def start(self, role):
        f = (LOG/(self.label+'-'+role+'-'+str(len(self.procs))+'.log')).open('w')
        self.files.append(f)
        proc = subprocess.Popen(['python3', '/app/worker.py', '--bootstrap', self.bootstrap,
                                 '--group', self.group, '--instance-id', self.ids[role],
                                 '--topic', self.aux if role=='lead' else self.topic,
                                 '--barrier-url', self.barrier.url, '--output-topic', self.output,
                                 '--processor-url', self.enrichment.url], cwd=tempfile.mkdtemp(prefix='worker-'),
                                env={**os.environ, 'PYTHONPATH': '/app'}, stdout=f, stderr=subprocess.STDOUT)
        self.procs.append(proc)
        return proc

    def complete(self, roles):
        v = self.view()
        if v['state'] != 'Stable' or set(v['members']) != {self.ids[r] for r in roles}: return False
        if (v['kind'], v['protocol']) != ('consumer', 'cooperative-sticky'): return False
        actual = collections.Counter(tuple(tp) for parts in v['members'].values() for tp in parts)
        expected = {(self.topic, p) for p in range(self.count)} | {(self.aux, 0)}
        return v if set(actual)==expected and all(n==1 for n in actual.values()) else False

    def ready(self):
        self.start('lead')
        wait_for(self.only_lead, 20, 'anchor failed to join')
        a = self.start('a')
        wait_for(lambda: self.complete(['lead', 'a']), 20, 'initial ownership not ready')
        self.progress()
        self.audit_armed = True
        return a

    def only_lead(self):
        v = self.view()
        return (v['state']=='Stable' and v['protocol']=='cooperative-sticky'
                and v['members']=={self.ids['lead']: [(self.aux, 0)]})

    def append(self, partitions=None, count=None):
        producer = Producer({'bootstrap.servers': self.bootstrap})
        failures = []
        for p in (range(self.count) if partitions is None else partitions):
            for n in range(count or (12 if self.enrichment.slow else 5)):
                value = f'{uuid.uuid4().hex}-{n}'.encode()
                def delivered(err, msg, value=value):
                    if err: failures.append(str(err))
                    else:
                        with self.data_lock:
                            self.inputs[(msg.topic(), msg.partition(), msg.offset())] = value
                producer.produce(self.topic, value=value, partition=p, on_delivery=delivered)
        assert producer.flush(10)==0 and not failures, f'producer failure: {failures}'

    def caught_up(self):
        with self.data_lock:
            inputs = dict(self.inputs)
            delivered = all(self.receipts.get(key)==value for key, value in inputs.items())
        offsets = self.coordinated(lambda broker: broker.offsets(self.group, self.topic, list(range(self.count))))
        return delivered and all(offsets.get(key[:2], -1) > key[2] for key in inputs)

    def progress(self):
        self.append()
        wait_for(self.caught_up, 20, 'receipts or committed offsets did not reach input high watermarks')

    def check_protocol_history(self):
        with self.lock: snapshots = list(self.history)
        for timestamp, v in snapshots:
            assert 'error' not in v, f'coordinator observation failed: {v}'
            if v['members']:
                assert v['kind']=='consumer', v
                if v['state']=='Stable': assert v['protocol']=='cooperative-sticky', v
                assert None not in v['members'], 'dynamic member in static group'
                assert set(v['members']) <= set(self.ids.values()), v
                counts = collections.Counter(tuple(tp) for parts in v['members'].values() for tp in parts)
                assert all(n==1 for n in counts.values()), f'duplicate assignments: {v}'

    def close(self):
        self.barrier.close()
        for p in self.procs:
            if p.poll() is None: p.kill()
        for p in self.procs: p.wait(10)
        self.done.set(); self.observer.join(10)
        for f in self.files: f.close()
        self.enrichment.close()
        with self.data_lock:
            evidence = dict(inputs=[dict(topic=k[0], partition=k[1], offset=k[2], value=v.hex())
                                    for k, v in self.inputs.items()],
                            delivered=[list(k) for k in self.receipts], violations=self.violations)
        (LOG/(self.label+'-data.json')).write_text(json.dumps(evidence, indent=2))


@pytest.mark.parametrize('fault', [signal.SIGTERM, signal.SIGKILL], ids=['drain', 'evict'])
def test_transfer_deployment(bootstrap, fault):
    case = Scenario(bootstrap, 'transfer-'+str(int(fault)), 6 if fault==signal.SIGTERM else 8, True)
    try:
        old = case.ready()
        case.enrichment.slow = True
        case.append(range(case.count//2, case.count), 12)
        wait_for(lambda: case.enrichment.busy(case.ids['a']), 10, 'load did not enter enrichment')
        case.start('b')
        try: facts, release = case.barrier.calls.get(timeout=20)
        except queue.Empty: pytest.fail('no nonempty cooperative revoke callback observed')
        assert facts['instance_id']==case.ids['a'] and facts['topic']==case.topic, facts
        reported = {(case.topic, p) for p in facts['partitions']}
        assert len(reported)==case.count//2, facts
        # This interval is shorter than the documented normal transfer allowance.
        hold_until = time.monotonic()+2
        while time.monotonic()<hold_until:
            v = case.view()
            owned = {tuple(tp) for parts in v['members'].values() for tp in parts}
            missing = {(case.topic, p) for p in range(case.count)} - owned
            assert v['state']=='Stable' and missing==reported, f'transfer did not remain withheld: {v}'
            assert set(v['members'])==set(case.ids.values()), v
            time.sleep(0.2)
        old.send_signal(fault)
        if fault==signal.SIGTERM:
            time.sleep(0.5); release.set()
            old.wait(15)
            assert old.returncode==0, f'graceful shutdown failed: {old.returncode}'
        else: old.wait(5)
        time.sleep(3)
        case.start('a')
        wait_for(lambda: case.complete(['lead', 'a', 'b']), 30,
                 'replacement did not restore exclusive full broker assignment')
        wait_for(case.caught_up, 20, 'work from before replacement was lost or stalled')
        case.progress()
        case.check_protocol_history()
        assert not case.violations, '\n'.join(case.violations[:12])
    finally: case.close()


@pytest.mark.parametrize('fault', [signal.SIGTERM, signal.SIGKILL], ids=['drain', 'evict'])
def test_quiet_deployment(bootstrap, fault):
    case = Scenario(bootstrap, 'quiet-'+str(int(fault)), 6, False)
    try:
        case.ready()
        old = case.start('b')
        baseline = wait_for(lambda: case.complete(['lead', 'a', 'b']), 20, 'scale-out failed')
        case.progress()
        case.enrichment.slow = True
        case.append(range(case.count//2, case.count), 12)
        wait_for(lambda: case.enrichment.busy(case.ids['b']), 10, 'load did not enter enrichment')
        time.sleep(1.5)
        begin = time.monotonic()
        old.send_signal(fault)
        old.wait(15)
        if fault==signal.SIGTERM: assert old.returncode==0, 'TERM did not exit cleanly'
        time.sleep(38)
        case.start('b')
        time.sleep(10)
        with case.lock: snapshots = [(t, v) for t, v in case.history if t>=begin]
        assert snapshots, 'observer did not cover the restart interval'
        for timestamp, view in snapshots:
            assert view==baseline, f'quiet deployment changed group state or assignments: {view} != {baseline}'
        wait_for(case.caught_up, 20, 'work from before replacement was lost or stalled')
        case.progress()
        case.check_protocol_history()
        assert not case.violations, '\n'.join(case.violations[:12])
    finally: case.close()

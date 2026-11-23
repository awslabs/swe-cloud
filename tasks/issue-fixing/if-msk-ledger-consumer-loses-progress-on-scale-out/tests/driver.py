import asyncio
import contextlib
import json
import os
import random
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import boto3
from botocore.config import Config
from confluent_kafka import Producer, libversion
from confluent_kafka.admin import AdminClient, NewTopic
from gateway import Gateway
from protocol import Reader, array, assignment, blob, num, string

LOG = Path('/logs/verifier')


def rpc(broker, api, version, body):
    header = num('h', api)+num('h', version)+num('i', 731)+string('observer')
    def exact(s, n):
        b = b''
        while len(b) < n:
            chunk = s.recv(n-len(b))
            if not chunk:
                raise OSError('broker closed observer connection')
            b += chunk
        return b
    with socket.create_connection(tuple([broker.rsplit(':', 1)[0], int(broker.rsplit(':', 1)[1])]), timeout=10) as s:
        s.sendall(blob(header+body))
        return Reader(exact(s, int.from_bytes(exact(s, 4), 'big'))[4:])


def offsets(broker, group, topic):
    r = rpc(broker, 9, 1, string(group)+array([string(topic)+array([num('i', p) for p in range(6)])]))
    out = {}
    for _ in range(r.n('i')):
        t = r.s()
        for _ in range(r.n('i')):
            p, o = r.n('i'), r.n('q')
            r.s()
            err = r.n('h')
            if err:
                raise RuntimeError(('OffsetFetch', err))
            out[p] = o
    return out


def log_ends(broker, topic):
    r = rpc(broker, 2, 1, num('i', -1)+array([string(topic)+array([
        num('i', p)+num('q', -1) for p in range(6)])]))
    out = {}
    for _ in range(r.n('i')):
        r.s()
        for _ in range(r.n('i')):
            p, err, timestamp, offset = r.n('i'), r.n('h'), r.n('q'), r.n('q')
            if err:
                raise RuntimeError(('ListOffsets', err))
            out[p] = offset
    return out


def describe(broker, group):
    r = rpc(broker, 15, 0, array([string(group)]))
    out = []
    for _ in range(r.n('i')):
        err, name, state, typ, proto = r.n('h'), r.s(), r.s(), r.s(), r.s()
        members = []
        for _ in range(r.n('i')):
            member, client, host, metadata = r.s(), r.s(), r.s(), r.bytes()
            members.append(dict(member=member, client=client, assignment=assignment(r.bytes())))
        out.append(dict(error=err, name=name, state=state, protocol=proto, members=members))
    return out[0]


def provision():
    assert libversion()[1] >= 0x020a00ff, libversion()
    config = Config(read_timeout=600, retries={'max_attempts': 1})
    k = boto3.client('kafka', endpoint_url=os.environ['AWS_ENDPOINT_URL'], config=config)
    suffix = uuid.uuid4().hex[:12]
    arn = k.create_cluster(ClusterName='audit-'+suffix, KafkaVersion='3.6.0',
        NumberOfBrokerNodes=1, BrokerNodeGroupInfo={'InstanceType': 'kafka.m5.large',
        'ClientSubnets': ['subnet-local'], 'StorageInfo': {'EbsStorageInfo': {'VolumeSize': 10}}})['ClusterArn']
    deadline = time.monotonic()+90
    while k.describe_cluster(ClusterArn=arn)['ClusterInfo']['State'] != 'ACTIVE':
        if time.monotonic() > deadline:
            raise RuntimeError('MSK not ACTIVE')
        time.sleep(1)
    broker = k.get_bootstrap_brokers(ClusterArn=arn)['BootstrapBrokerString']
    topic, group = 'entries-'+suffix, 'fleet-'+suffix
    for i in range(20):
        try:
            admin = AdminClient({'bootstrap.servers': broker})
            f = admin.create_topics([NewTopic(topic, 6, 1)])
            f[topic].result(10)
            break
        except Exception:
            if i == 19:
                raise
            time.sleep(1)
    ddb = boto3.resource('dynamodb', endpoint_url=os.environ['AWS_ENDPOINT_URL'])
    table = ddb.create_table(TableName='observations-'+suffix, BillingMode='PAY_PER_REQUEST',
        KeySchema=[{'AttributeName': 'pk', 'KeyType': 'HASH'}, {'AttributeName': 'sk', 'KeyType': 'RANGE'}],
        AttributeDefinitions=[{'AttributeName': 'pk', 'AttributeType': 'S'}, {'AttributeName': 'sk', 'AttributeType': 'N'}])
    table.wait_until_exists()
    return broker, topic, group, table


async def run():
    LOG.mkdir(parents=True, exist_ok=True)
    try:
        broker, topic, group, table = await asyncio.to_thread(provision)
        # DescribeGroups and OffsetFetch must be supported independently of the worker.
        await asyncio.to_thread(offsets, broker, group, topic)
        await asyncio.to_thread(describe, broker, group)
    except Exception as e:
        (LOG/'environment').write_text(repr(e))
        raise
    effects = []
    calls = []
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                assert self.path == '/apply'
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                assert set(body) == {'worker_id', 'member_id', 'partition', 'offset'}
                with lock:
                    calls.append(dict(body, ts=time.monotonic()))
                # Accounting has independent lanes. Slow entries do not hold up
                # later entries, and no broker response is manufactured here.
                time.sleep(1.25 if body['offset'] % 3 == 0 else .015)
                with lock:
                    e = dict(body, seq=len(effects), ts=time.monotonic())
                    table.put_item(Item={'pk': 'effect', 'sk': e['seq'], 'body': json.dumps(e)})
                    effects.append(e)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'ok')
            except Exception as e:
                self.send_error(500, str(e))

        def log_message(self, *_):
            pass

    class AccountingServer(ThreadingHTTPServer):
        request_queue_size = 128

    http = AccountingServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    producer = Producer({'bootstrap.servers': broker, 'client.id': 'operations', 'linger.ms': 0})
    produced = []

    def produce_sync(p):
        out = []
        def ack(err, msg):
            if err:
                raise RuntimeError(err)
            out.append(msg.offset())
        producer.produce(topic, partition=p, value=uuid.uuid4().bytes, on_delivery=ack)
        producer.flush(10)
        if not out:
            raise RuntimeError('producer ack missing')
        produced.append(dict(partition=p, offset=out[0], ts=time.monotonic()))
        return out[0]

    async def produce(p):
        return await asyncio.to_thread(produce_sync, p)

    gateway = Gateway(broker, topic, produce)
    await gateway.start()
    processes, files = {}, []

    def launch(w):
        log = open(LOG/(w+'.log'), 'w')
        files.append(log)
        processes[w] = subprocess.Popen(['python3', '/app/worker.py', '--worker-id', w,
            '--bootstrap', f'127.0.0.1:{gateway.port}', '--group', group, '--topic', topic,
            '--effects-url', f'http://127.0.0.1:{http.server_port}'],
            cwd=tempfile.mkdtemp(prefix='ledger-role-'),
            env={**os.environ, 'PYTHONPATH': '/app', 'PYTHONUNBUFFERED': '1'}, stdout=log, stderr=log)

    async def until(predicate, seconds):
        end = time.monotonic()+seconds
        while time.monotonic() < end:
            if predicate():
                return True
            await asyncio.sleep(.05)
        return False

    status = {}
    try:
        launch('W1')
        for p in random.sample(range(6), 6):
            for _ in range(random.randint(2, 4)):
                await produce(p)
        ends = {p: max(e['offset'] for e in produced if e['partition'] == p)+1 for p in range(6)}
        warm = False
        deadline = time.monotonic()+35
        while time.monotonic() < deadline:
            got = await asyncio.to_thread(offsets, broker, group, topic)
            if got == ends:
                warm = True
                break
            if processes['W1'].poll() is not None:
                break
            await asyncio.sleep(.15)
        status['warm'] = warm
        if warm:
            # Exercise the production capacity contract after ownership is stable.
            hot = random.randrange(6)
            for _ in range(18):
                o = await produce(hot)
                gateway.log('produced_burst', partition=hot, offset=o, owner='W1')
            ends[hot] += 18
            deadline = time.monotonic()+20
            caught = False
            while time.monotonic() < deadline:
                if await asyncio.to_thread(offsets, broker, group, topic) == ends:
                    caught = True
                    break
                if processes['W1'].poll() is not None:
                    break
                await asyncio.sleep(.1)
            status['burst_quiesced'] = caught
            launch('W2')
            await until(lambda: gateway.phase == 'quiet' or any(p.poll() is not None for p in processes.values()), 45)
            if gateway.phase == 'quiet':
                for p in random.sample(sorted(gateway.V), len(gateway.V)):
                    for _ in range(18):
                        o = await produce(p)
                        gateway.log('produced_tail', partition=p, offset=o)
            # Observe stability even if the worker has died; never let an early failure erase evidence.
            await asyncio.sleep(15)
        status['alive'] = {w: p.poll() is None for w, p in processes.items()}
        status['describe'] = await asyncio.to_thread(describe, broker, group)
        status['log_ends'] = await asyncio.to_thread(log_ends, broker, topic)
        status['offsets'] = await asyncio.to_thread(offsets, broker, group, topic)
        status['phase'] = gateway.phase
        status['gateway_errors'] = gateway.errors
        status['baseline_generation'] = gateway.gen
        status['members'] = gateway.members
        status['revoked'], status['retained'] = sorted(gateway.V), sorted(gateway.R)
        status['markers'] = gateway.markers
    finally:
        # The graded quiet window ends before normal shutdown creates LeaveGroup traffic.
        wire = list(gateway.events)
        status['observed_until'] = time.monotonic()
        for p in processes.values():
            if p.poll() is None:
                p.terminate()
        for p in processes.values():
            try:
                await asyncio.to_thread(p.wait, 15)
            except subprocess.TimeoutExpired:
                p.kill()
                await asyncio.to_thread(p.wait)
        for f in files:
            f.close()
        await gateway.close()
        await asyncio.to_thread(http.shutdown)
        # Persist the independent observer's history, then read it back for all grading.
        with table.batch_writer() as batch:
            for e in wire:
                batch.put_item(Item={'pk': 'wire', 'sk': e['seq'], 'body': json.dumps(e)})
            batch.put_item(Item={'pk': 'status', 'sk': 0, 'body': json.dumps(status)})
            for i, e in enumerate(calls):
                batch.put_item(Item={'pk': 'call', 'sk': i, 'body': json.dumps(e)})
            for i, e in enumerate(produced):
                batch.put_item(Item={'pk': 'produced', 'sk': i, 'body': json.dumps(e)})
    items = []
    result = table.scan(ConsistentRead=True)
    items.extend(result['Items'])
    while 'LastEvaluatedKey' in result:
        result = table.scan(ConsistentRead=True, ExclusiveStartKey=result['LastEvaluatedKey'])
        items.extend(result['Items'])
    def history(pk):
        return [json.loads(e['body']) for e in sorted(items, key=lambda e: int(e['sk'])) if e['pk'] == pk]
    result = dict(wire=history('wire'), calls=history('call'), effects=history('effect'), produced=history('produced'), status=history('status')[0], topic=topic)
    (LOG/'observations.json').write_text(json.dumps(result, indent=2))
    return result

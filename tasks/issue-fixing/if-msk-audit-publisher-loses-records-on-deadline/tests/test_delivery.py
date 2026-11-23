"""Historical HTTP outcomes checked against broker-owned records and gateway trace."""
import concurrent.futures
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from collections import Counter
import pytest
from broker_env import provision
import broker_wire as wire
from gateway import Gateway

OUT = Path('/logs/verifier')


def request(port, path, data=None, timeout=20):
    body = None if data is None else json.dumps(data).encode()
    req = urllib.request.Request('http://127.0.0.1:%s%s' % (port, path), data=body,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)


def unused_port():
    import socket
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); return s.getsockname()[1]


def preflight():
    try:
        resource = provision()
        bootstrap = resource['bootstrap']
        name = 'probe-' + uuid.uuid4().hex[:10]
        wire.create_topic(bootstrap, name)
        pid, epoch = wire.init_producer_id(bootstrap)
        first = wire.produce(bootstrap, name, 0, pid, epoch, 0, [('probe', 'one')])
        duplicate = wire.produce(bootstrap, name, 0, pid, epoch, 0, [('other', 'two')])
        gap = wire.produce(bootstrap, name, 0, pid, epoch, 2, [('gap', 'three')])
        if first[0] != 0 or duplicate != first or gap[0] != 45:
            raise RuntimeError('broker idempotence preflight: %r' % ((first, duplicate, gap),))
        return resource
    except Exception as e:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / 'environment').write_text(repr(e))
        raise


@pytest.fixture(scope='module')
def observed():
    OUT.mkdir(parents=True, exist_ok=True)
    resource = preflight()
    (OUT / 'resource.json').write_text(json.dumps(resource))
    gateway = Gateway(resource['bootstrap'])
    port = unused_port()
    log = open(OUT / 'publisher.log', 'w')
    process = subprocess.Popen(['python3', '/app/publisher.py'], cwd=tempfile.mkdtemp(prefix='publisher-'),
        env={**os.environ, 'PYTHONPATH': '/app', 'KAFKA_BOOTSTRAP': gateway.address,
             'TOPIC': 'ledger', 'PORT': str(port)}, stdout=log, stderr=subprocess.STDOUT)
    ledger = []
    ledger_lock = threading.Lock()
    seed = random.SystemRandom().randrange(2**32)
    rng = random.Random(seed)
    (OUT / 'seed.txt').write_text(str(seed))

    def publish(partition=0, deadline=6000, key=None, healthy=True, barrier=None):
        key = key or uuid.uuid4().hex
        data = dict(id=key, partition=partition, value='audit/' + uuid.uuid4().hex + '/\u03bb', deadline_ms=deadline)
        if barrier: barrier.wait(timeout=10)
        sent = time.monotonic()
        try:
            reply = request(port, '/publish', data, timeout=deadline/1000+10)
        except Exception as e:
            reply = {'status': 'transport_error', 'error': repr(e)}
        row = dict(data, sent=sent, answered=time.monotonic(), reply=reply, healthy=healthy)
        with ledger_lock:
            ledger.append(row)
            with open(OUT / 'ledger.jsonl', 'a') as f: f.write(json.dumps(row) + '\n')
        return row

    try:
        end = time.monotonic() + 30
        while True:
            try:
                request(port, '/healthz', timeout=.5)
                break
            except Exception:
                if process.poll() is not None or time.monotonic() > end:
                    pytest.fail('publisher failed readiness; see publisher.log')
                time.sleep(.1)
        schedule = [(mode, code) for mode in ('append', 'drop', 'late') for code in (6, 7, 20, 56)]
        rng.shuffle(schedule)
        for mode, code in schedule:
            publish()
            key = uuid.uuid4().hex
            gateway.arm(key, 0, mode, code)
            publish(key=key, deadline=2000, healthy=False)
            publish()
            for _ in range(3): publish()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            for _ in range(3):
                publish(0); publish(1)
                a, x = uuid.uuid4().hex, uuid.uuid4().hex
                gateway.arm(a, 0, 'append', 6)
                gateway.arm(x, 1, 'append', 7, hold=2.3, sticky=0)
                barrier = threading.Barrier(2)
                fa = pool.submit(publish, 0, 2000, a, False, barrier)
                fx = pool.submit(publish, 1, 6000, x, True, barrier)
                fa.result(timeout=25); fx.result(timeout=25)
                publish(0); publish(1)
        # A held response must not serialize the rest of an admitted window.
        # More calls then queue behind that window; no assumption is made about
        # their exact dispatch order or retry count.
        def wait_for_send(key, stored=False):
            end = time.monotonic() + 10
            while time.monotonic() < end:
                events = gateway.snapshot()
                matching = [e for e in events if key in e.get('keys', [])]
                if matching and (not stored or any(e.get('broker_code') == 0 for e in matching)):
                    return
                time.sleep(.01)
            raise RuntimeError('publisher never sent an admitted event: ' + key)

        with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            for partition in (0, 1):
                publish(partition)
                head = uuid.uuid4().hex
                gateway.arm(head, partition, 'append', 7, hold=3.2, sticky=0)
                held = pool.submit(publish, partition, 6000, head)
                wait_for_send(head, stored=True)
                followers = [pool.submit(publish, partition, 2000) for _ in range(3)]
                for future in followers:
                    future.result(timeout=15)
                more = [pool.submit(publish, partition, 9000) for _ in range(9)]
                held.result(timeout=15)
                for future in more:
                    future.result(timeout=20)

            # A predecessor can disappear, persist, or arrive after cancellation.
            # Its live suffix can already have appended, or be sequence-rejected.
            for mode in ('append', 'drop', 'late'):
                partition = rng.randrange(2)
                publish(partition)
                a, b, c = [uuid.uuid4().hex for _ in range(3)]
                gateway.arm(a, partition, mode, rng.choice((6, 7, 20, 56)))
                fa = pool.submit(publish, partition, 2000, a, False)
                wait_for_send(a, stored=mode == 'append')
                gateway.arm(b, partition, 'append', 7, hold=3.2, sticky=0)
                gateway.arm(c, partition, 'append', 20, hold=3.3, sticky=0)
                fb = pool.submit(publish, partition, 7000, b)
                fc = pool.submit(publish, partition, 7000, c)
                fd = pool.submit(publish, partition, 7000)
                fa.result(timeout=15)
                fe = pool.submit(publish, partition, 7000)
                for future in (fb, fc, fd, fe):
                    future.result(timeout=20)
                publish(partition)

            # The canceled call need not be the first unresolved range.
            for partition in (0, 1):
                head, a, tail = [uuid.uuid4().hex for _ in range(3)]
                gateway.arm(head, partition, 'append', 7, hold=3.2, sticky=0)
                fh = pool.submit(publish, partition, 7000, head)
                wait_for_send(head, stored=True)
                gateway.arm(a, partition, 'drop', 56)
                fa = pool.submit(publish, partition, 2000, a, False)
                wait_for_send(a)
                gateway.arm(tail, partition, 'append', 7, hold=3.3, sticky=0)
                ft = pool.submit(publish, partition, 7000, tail)
                fa.result(timeout=15)
                after = pool.submit(publish, partition, 7000)
                for future in (fh, ft, after):
                    future.result(timeout=20)
                publish(partition)

            # A live call's request is delayed in the network. A short-deadline
            # call admitted behind it expires meanwhile; the others stay live.
            partition = rng.randrange(2)
            publish(partition)
            slow, brief, tail = [uuid.uuid4().hex for _ in range(3)]
            gateway.arm(slow, partition, 'late', 7, sticky=0)
            fs = pool.submit(publish, partition, 7000, slow)
            wait_for_send(slow)
            fb = pool.submit(publish, partition, 500, brief, False)
            ft = pool.submit(publish, partition, 7000, tail)
            fb.result(timeout=15)
            after = pool.submit(publish, partition, 7000)
            for future in (fs, ft, after):
                future.result(timeout=20)
            publish(partition)

        time.sleep(10)
        records = []
        for partition in (0, 1):
            offset = 0
            end = time.monotonic() + 20
            while True:
                high, rows = wire.fetch(resource['bootstrap'], 'ledger', partition, offset)
                for row in rows:
                    if row['offset'] >= offset:
                        records.append(dict(row, partition=partition))
                if rows: offset = max(offset, max(r['offset'] for r in rows)+1)
                if offset >= high: break
                if time.monotonic() > end: raise RuntimeError('could not fetch through high watermark')
                time.sleep(.1)
        events = gateway.snapshot()
        (OUT / 'broker.json').write_text(json.dumps(records, indent=2))
        (OUT / 'gateway.json').write_text(json.dumps(events, indent=2))
        yield dict(ledger=ledger, records=records, events=events, gateway_errors=list(gateway.errors))
    finally:
        process.terminate()
        try: process.wait(timeout=5)
        except subprocess.TimeoutExpired: process.kill(); process.wait()
        log.close()
        (OUT / 'gateway.json').write_text(json.dumps(gateway.snapshot(), indent=2))
        gateway.close()


def test_acknowledgements_name_the_actual_event(observed):
    index = {(r['partition'], r['offset']): r for r in observed['records']}
    errors = []
    for row in observed['ledger']:
        reply = row['reply']
        if reply.get('status') != 'acked': continue
        actual = index.get((reply.get('partition'), reply.get('offset')))
        if (reply.get('partition') != row['partition'] or actual is None or
            actual['key'] != row['id'] or actual['value'] != row['value']):
            errors.append(dict(id=row['id'], reply=reply, actual=actual))
    assert not errors, json.dumps(errors, indent=2)


def test_records_are_never_duplicated(observed):
    counts = Counter(r['key'] for r in observed['records'])
    duplicates = {key: n for key, n in counts.items() if n > 1}
    assert not duplicates, duplicates
    known = {row['id']: row for row in observed['ledger']}
    assert set(counts) <= set(known), 'unexpected records in the audit topic'
    malformed = [r for r in observed['records'] if r['value'] != known[r['key']]['value']
                 or r['partition'] != known[r['key']]['partition']]
    assert not malformed, malformed


def test_deadlines_and_http_outcomes(observed):
    errors = []
    for row in observed['ledger']:
        elapsed = (row['answered'] - row['sent']) * 1000
        reply = row['reply']
        if (elapsed > row['deadline_ms'] + 500 or reply.get('status') not in ('acked', 'failed')
            or (reply.get('status') == 'failed' and not isinstance(reply.get('error'), str))):
            errors.append(dict(id=row['id'], elapsed_ms=elapsed, reply=reply))
    assert not errors, errors


def test_healthy_traffic_continues(observed):
    errors = [row for row in observed['ledger'] if row['healthy'] and row['reply'].get('status') != 'acked']
    assert not errors, errors


def test_failed_calls_stop_sending(observed):
    errors = []
    sends = [e for e in observed['events'] if e['api'] == 0]
    for row in observed['ledger']:
        if row['reply'].get('status') != 'failed': continue
        after = [e for e in sends if row['id'] in e['keys'] and e['time'] > row['answered'] + .100]
        if after: errors.append(dict(id=row['id'], after=after))
    assert not errors, errors


def test_wire_contract_and_allocation_quota(observed):
    events = observed['events']
    sends = [e for e in events if e['api'] == 0]
    failed_sent = sum(row['reply'].get('status') == 'failed' and any(
        row['id'] in e['keys'] and e['time'] <= row['answered'] for e in sends)
        for row in observed['ledger'])
    quota = 2 + failed_sent
    identities = {(e['pid'], e['epoch']) for e in sends}
    identities.update((r['pid'], r['epoch']) for r in observed['records'])
    init_calls = [e for e in events if e['api'] == 22]
    assert len(identities) <= quota, (len(identities), quota)
    assert len(init_calls) <= quota, (len(init_calls), quota)
    assert sends, 'publisher made no Produce requests'
    assert all(r['pid'] >= 0 and not r['attrs'] & 16 for r in observed['records']), 'invalid broker batch header'
    assert all(e['pid'] >= 0 and e['acks'] == -1 and not e['attrs'] & 16 and
               e['transactional_id'] is None for e in sends), 'non-idempotent or transactional Produce'
    assert not [e for e in events if e.get('violation') or e.get('transactional_id') is not None]
    assert not observed['gateway_errors'], observed['gateway_errors']

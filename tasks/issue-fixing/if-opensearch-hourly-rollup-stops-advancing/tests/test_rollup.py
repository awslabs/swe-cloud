import json
import math
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import uuid

from engine import Engine, provision, oracle, target_docs, SOURCES
from gateway import Gateway

MINUTE = 60000
HOUR = 60 * MINUTE
T0 = 1745193600000
LOG = Path('/logs/verifier')


def dump(name, obj):
    LOG.mkdir(parents=True, exist_ok=True)
    (LOG / name).write_text(json.dumps(obj, indent=2, default=str))


def probes(gateway, engine, source):
    composite = {'rows': {'composite': {'size': 1000, 'sources': SOURCES}}}
    masked = {'size': 0, 'query': {'match_all': {}}, 'aggs': composite,
              'post_filter': {'match_none': {}}}
    global_body = {'size': 0, 'query': {'match_none': {}},
                   'aggs': {'all': {'global': {}, 'aggs': {'m': {'sum': {'field': 'm1'}}}}}}
    # Confirm the two billing hazards are real on the pinned engine.
    direct_post = engine.call('POST', source + '/_search', {**masked, 'track_total_hits': True})
    direct_global = engine.call('POST', source + '/_search', {**global_body, 'track_total_hits': True})
    assert direct_post['hits']['total']['value'] == 0
    assert direct_post['aggregations']['rows']['buckets']
    assert direct_global['hits']['total']['value'] == 0
    assert direct_global['aggregations']['all']['doc_count'] > 12000
    request_shapes = [
        (masked, {}), (global_body, {}),
        ({'size': 0, 'aggs': composite}, {'filter_path': 'aggregations'}),
        ({'size': 0, 'aggs': composite}, {'terminate_after': '1'}),
        ({'size': 0, 'aggs': composite, 'min_score': 1e9}, {}),
        ({'size': 0, 'aggs': composite}, {'q': '*'}),
        ({'aggs': {'s': {'sum': {'script': '1'}}}}, {}),
        ({'query': {'terms': {'device': {'index': source, 'id': 'x', 'path': 'device'}}}}, {}),
        ({'size': 0, 'aggs': {'f': {'filter': {'match_all': {}}, 'aggs': {'g': {'global': {}}}}}}, {}),
        ({'query': {'bool': {'filter': [{'script': {'script': 'true'}}]}}}, {}),
        ({'size': 0, 'aggs': composite}, {'search_pipeline': 'anything'})]
    before = engine.search_total(source)
    proxy = Engine(gateway.url)
    for body, params in request_shapes:
        try:
            proxy.call('POST', source + '/_search', body, **params)
        except urllib.error.HTTPError as exc:
            assert exc.code == 400, (body, exc.code)
        else:
            raise AssertionError('environment: gateway accepted a masking shape')
    assert before == engine.search_total(source), 'environment: rejected probe reached engine'
    # Check the admission boundary against real target state before grading a worker.
    gateway.reset()
    ids = ['admission-probe-' + str(i) for i in range(10)]
    actions = []
    for identifier in ids:
        actions.extend(({'index': {'_index': gateway.target, '_id': identifier}},
                        {'bucket_start_ms': 0, 'device': 'probe', 'doc_count': 0}))
    raw = ('\n'.join(json.dumps(x) for x in actions) + '\n').encode()
    result = proxy.call('POST', '_bulk', raw)
    statuses = [next(iter(x.values()))['status'] for x in result['items']]
    assert result['errors'] and all(200 <= x < 300 for x in statuses[:8])
    assert statuses[8:] == [429, 429], ('environment: bulk admission', statuses)
    try:
        proxy.call('PUT', gateway.target + '/_doc/' + ids[8], {'doc_count': 0})
    except urllib.error.HTTPError as exc:
        assert exc.code == 429, ('environment: allocation did not persist', exc.code)
    else:
        raise AssertionError('environment: allocation refilled within an invocation')
    engine.call('POST', gateway.target + '/_refresh')
    actual = target_docs(engine, gateway.target)
    assert all(identifier in actual for identifier in ids[:8])
    assert all(identifier not in actual for identifier in ids[8:])
    for identifier in ids[:8]:
        engine.call('DELETE', gateway.target + '/_doc/' + identifier)
    engine.call('POST', gateway.target + '/_refresh')
    # Native concurrency primitives must survive the gateway unchanged.
    admission_log = list(gateway.log)
    gateway.reset()
    path = gateway.meta + '/_doc/' + gateway.transform
    prior = proxy.call('GET', path)
    condition = {'if_seq_no': prior['_seq_no'], 'if_primary_term': prior['_primary_term']}
    updated = proxy.call('PUT', path, prior['_source'], **condition)
    try:
        proxy.call('PUT', path, {**prior['_source'], 'status': 'bad-stale-write'}, **condition)
    except urllib.error.HTTPError as exc:
        assert exc.code == 409
    else:
        raise AssertionError('environment: stale metadata condition accepted')
    assert proxy.call('GET', path)['_source'] == prior['_source']
    identifier = 'external-version-probe'
    target_path = gateway.target + '/_doc/' + identifier
    proxy.call('PUT', target_path, {'doc_count': 1}, version=10, version_type='external')
    proxy.call('PUT', target_path, {'doc_count': 2}, version=10, version_type='external_gte')
    for version, kind in ((9, 'external_gte'), (10, 'external')):
        try:
            proxy.call('PUT', target_path, {'doc_count': 99}, version=version, version_type=kind)
        except urllib.error.HTTPError as exc:
            assert exc.code == 409
        else:
            raise AssertionError('environment: stale external version accepted')
    item = proxy.call('GET', target_path)
    assert item['_version'] == 10 and item['_source']['doc_count'] == 2
    assert gateway.credit == 4, 'environment: conflicts did not spend target credit'
    engine.call('DELETE', target_path)
    engine.call('POST', gateway.target + '/_refresh')
    dump('gateway-preflight.json', {'admission': admission_log, 'concurrency': gateway.log})


class Scenario:
    def __init__(self, engine):
        self.e = engine
        suffix = uuid.uuid4().hex[:8]
        self.source, self.target, self.meta = ['metrics-' + kind + '-' + suffix for kind in ('src', 'hourly', 'meta')]
        self.transform = 'hourly-' + suffix
        self.rng = random.Random(37129)
        self.routes = {}
        self.documents = {}
        self.corrections = []
        self.injected = []
        self.failures = []
        self.runs = []
        self.now = T0
        self.archived = []
        self.revoked = []
        self.restored = []
        self.gateway = None

    def check(self, ok, message):
        if not ok:
            self.failures.append(message)

    def create(self):
        self.e.call('PUT', self.source, {
            'settings': {'number_of_shards': 3, 'number_of_replicas': 0, 'refresh_interval': '-1'},
            'mappings': {'properties': {'ts': {'type': 'date', 'format': 'epoch_millis'},
                                       'device': {'type': 'keyword'}, 'm1': {'type': 'long'},
                                       'voided': {'type': 'boolean'}}}})
        self.e.call('PUT', self.target, {'settings': {'number_of_shards': 1, 'number_of_replicas': 0},
            'mappings': {'properties': {'bucket_start_ms': {'type': 'date', 'format': 'epoch_millis'},
                'device': {'type': 'keyword'}, 'doc_count': {'type': 'long'},
                'm1_sum': {'type': 'double'}, 'm1_count': {'type': 'long'}, 'm1_avg': {'type': 'double'}}}})
        self.e.call('PUT', self.meta, {'settings': {'number_of_shards': 1, 'number_of_replicas': 0}})
        for n in range(100):
            route = 'route-' + str(n)
            shards = self.e.call('GET', self.source + '/_search_shards', routing=route)['shards']
            shard = str(shards[0][0]['shard'])
            self.routes[shard] = route
            if len(self.routes) == 3:
                break
        assert len(self.routes) == 3, 'environment: could not identify shard routes'

    def action(self, identifier, doc, route):
        self.documents[identifier] = (dict(doc), route)
        return [{'index': {'_index': self.source, '_id': identifier, 'routing': route}}, doc]

    def seed(self):
        actions = []
        for minute in range(72 * 60):
            ts = T0 - 72 * HOUR + minute * MINUTE
            for device in range(3):
                identifier = f'event-{minute}-{device}'
                shard = self.rng.choices(['0', '1', '2'], weights=[6, 3, 1])[0]
                doc = {'ts': ts, 'device': 'dev-' + str(device)}
                if (minute * 3 + device) % 97:
                    doc['m1'] = self.rng.randrange(-4, 100)
                actions += self.action(identifier, doc, self.routes[shard])
                if ts < T0 - 4 * HOUR:
                    self.corrections.append(identifier)
            if minute % 60 == 30:
                identifier = f'quiet-{minute}'
                doc = {'ts': ts, 'device': 'dev-q', 'm1': 7}
                actions += self.action(identifier, doc, self.routes[str(minute // 60 % 3)])
            if len(actions) >= 4000:
                self.e.bulk(actions)
                actions = []
        if actions:
            self.e.bulk(actions)
        # Sparse buckets make last-event removal observable, including a no-metric event.
        for n in range(12):
            doc = {'ts': T0 - (5 + n) * HOUR + 123, 'device': 'revocable-' + str(n)}
            if n % 3:
                doc['m1'] = 50 + n
            self.e.bulk(self.action('revocable-' + str(n), doc, self.routes[str(n % 3)]))
        checkpoints = self.e.settle(self.source)
        upper = self.now - HOUR
        backfill = oracle(self.e, self.source, upper, self.transform)
        writes = []
        for identifier, doc in backfill.items():
            writes += [{'index': {'_index': self.target, '_id': identifier}}, doc]
        self.e.bulk(writes)
        self.e.call('PUT', self.meta + '/_doc/' + self.transform,
                    {'shard_checkpoints': checkpoints, 'selection_upper_bound_ms': upper, 'status': 'committed'})
        self.e.call('POST', self.target + '/_refresh')
        self.e.call('POST', self.meta + '/_refresh')
        search_before = self.e.search_total(self.source)
        probe = self.e.call('POST', self.source + '/_search', {
            'size': 0, 'track_total_hits': True,
            'query': {'range': {'_seq_no': {'gte': 0, 'lte': checkpoints['2']}}},
            'aggs': {'rows': {'composite': {'sources': SOURCES, 'size': 1000}}}}, preference='_shards:2')
        assert probe['hits']['total']['relation'] == 'eq' and probe['hits']['total']['value'] > 0
        search_delta = self.e.search_total(self.source) - search_before
        assert search_delta == probe['_shards']['total'], 'environment: source search statistics do not meter shard requests'
        dump('engine-preflight.json', {'checkpoints': checkpoints, 'source_docs': len(self.documents),
             'seq_range_hits': probe['hits']['total'], 'search_counter_delta': search_delta, 'shards_searched': probe['_shards']})
        self.gateway = Gateway(self.e, self.source, self.target, self.meta, self.transform)
        probes(self.gateway, self.e, self.source)
        self.gateway.reset()

    def archive_and_revoke(self, step):
        actions = []
        lower = self.now - 49 * HOUR
        expired = [key for key, (doc, _) in self.documents.items() if doc['ts'] <= lower]
        for key in expired:
            doc, route = self.documents.pop(key)
            actions.append({'delete': {'_index': self.source, '_id': key, 'routing': route}})
            self.archived.append(key)
        self.corrections = [key for key in self.corrections if key in self.documents]
        # Revocation can empty a sparse bucket; restoration can refill the same stable ID.
        key = 'revocable-' + str(step % 12)
        if key in self.documents:
            doc, route = self.documents[key]
            actions += self.action(key, {**doc, 'voided': True}, route)
            self.revoked.append(key)
        if step >= 3:
            restore = 'revocable-' + str((step - 2) % 12)
            if restore in self.documents:
                doc, route = self.documents[restore]
                actions += self.action(restore, {**doc, 'voided': False}, route)
                self.restored.append(restore)
        if actions:
            self.e.bulk(actions)
        return self.e.settle(self.source)

    def ingest(self, step, minutes):
        old = self.now
        self.now += minutes * MINUTE
        actions = []
        for ts in range(old, self.now, MINUTE):
            for device in range(3):
                identifier = f'live-{ts}-{device}'
                doc = {'ts': ts, 'device': 'dev-' + str(device), 'm1': 11 + device}
                shard = self.rng.choices(['0', '1', '2'], weights=[6, 3, 1])[0]
                actions += self.action(identifier, doc, self.routes[shard])
        for j in range(5):
            age_minutes = self.rng.randrange(25 * 60, 48 * 60) if j < 2 else self.rng.randrange(3 * 60, 24 * 60)
            ts = self.now - age_minutes * MINUTE
            doc = {'ts': ts, 'device': 'dev-' + str(j % 3), 'm1': 101 + j}
            actions += self.action(f'delivery-{step}-{j}', doc, self.routes['2'])
        for identifier in self.rng.sample(self.corrections, 3):
            doc, route = self.documents[identifier]
            doc = {**doc, 'm1': doc.get('m1', 0) + 401 + step}
            actions += self.action(identifier, doc, route)
        self.e.bulk(actions)
        return self.archive_and_revoke(step)

    def inject(self):
        n = len(self.injected)
        if n >= 24:
            return
        # Each write changes a quiet bucket that discovery need not have visited yet.
        ts = T0 - (6 + n) * HOUR + 15 * MINUTE
        identifier = 'ingress-' + str(n)
        doc = {'ts': ts, 'device': 'dev-q', 'm1': 17}
        self.e.bulk(self.action(identifier, doc, self.routes[str(n % 3)]))
        self.e.settle(self.source)
        self.injected.append(identifier)

    def progress(self):
        return self.e.call('GET', self.meta + '/_doc/' + self.transform)['_source']

    def invoke(self, step, attempt, kill=False, inject=False):
        g = self.gateway
        g.reset()
        g.inject = self.inject if inject else None
        g.kill_write = kill
        previous = self.progress()
        def at_kill():
            current = self.progress()
            for field in ('shard_checkpoints', 'selection_upper_bound_ms'):
                self.check(current.get(field) == previous.get(field),
                           f'run {step}: committed {field} advanced before interrupted target write')
        g.on_kill = at_kill
        before = self.e.search_total(self.source)
        prior_forwarded = sum(r['forwarded_shards'] for r in self.runs)
        prior_observed = self.e.observed_shards.get(self.source, 0)
        env = {**os.environ, 'OS_ENDPOINT': g.url, 'SOURCE_INDEX': self.source,
               'TARGET_INDEX': self.target, 'META_INDEX': self.meta, 'TRANSFORM_ID': self.transform,
               'PYTHONPATH': '/app', 'PYTHONDONTWRITEBYTECODE': '1'}
        log_path = LOG / f'worker-{step}-{attempt}.txt'
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix='run-') as cwd, log_path.open('w') as output:
            worker = subprocess.Popen([sys.executable, '-m', 'transform_runner', 'run_once',
                                       '--now-ms', str(self.now)], cwd=cwd, env=env,
                                       stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            g.process = worker
            g.process_ready.set()
            try:
                rc = worker.wait(timeout=120)
            except subprocess.TimeoutExpired:
                os.killpg(worker.pid, signal.SIGKILL)
                worker.wait(timeout=10)
                rc = 'timeout'
        # Overlap includes completed peers and verifier oracle observations; charge neither
        # to the abandoned worker when reconciling the engine search counter.
        delta = self.e.search_total(self.source) - before
        peers = sum(r['forwarded_shards'] for r in self.runs) - prior_forwarded
        observations = self.e.observed_shards.get(self.source, 0) - prior_observed
        record = {'run': step, 'attempt': attempt, 'rc': rc, 'elapsed': time.monotonic() - started,
                  'docs_read': g.docs, 'engine_query_delta': delta, 'peer_forwarded_shards': peers,
                  'observer_shards': observations, 'forwarded_shards': g.shards,
                  'killed': g.killed, 'accepted': list(g.accepted),
                  'requests': list(g.log), 'gateway_errors': list(g.errors)}
        self.runs.append(record)
        dump('runs.json', self.runs)
        self.check(not g.errors, f'run {step}: gateway errors {g.errors}')
        self.check(delta - peers - observations <= g.shards,
                   f'run {step}: source reads bypassed gateway: {delta} - {peers} - {observations} > {g.shards}')
        self.check(g.docs <= 3000, f'run {step}/{attempt}: read allowance exceeded: {g.docs} > 3000')
        if kill:
            self.check(g.killed, f'run {step}: no target write reached interruption boundary')
            if g.killed:
                self.check(rc != 0, f'run {step}: interrupted process returned success')
        else:
            self.check(rc in (0, 75), f'run {step}/{attempt}: worker exit {rc}; see {log_path.name}')
        return rc

    def compare(self, step, snapshot, equality=True):
        self.e.call('POST', self.target + '/_refresh')
        self.e.call('POST', self.meta + '/_refresh')
        current = self.progress()
        self.check(current.get('shard_checkpoints') == snapshot,
                   f'run {step}: checkpoint snapshot differs: expected {snapshot}, got {current.get("shard_checkpoints")}')
        self.check(current.get('selection_upper_bound_ms') == self.now - HOUR,
                   f'run {step}: selection boundary differs: expected {self.now - HOUR}, got {current.get("selection_upper_bound_ms")}')
        self.check(current.get('status') == 'committed', f'run {step}: status is not committed')
        if equality:
            expected = oracle(self.e, self.source, self.now - HOUR, self.transform)
            actual = target_docs(self.e, self.target)
            zero = {key: doc for key, doc in actual.items() if doc.get('doc_count') == 0}
            for key, doc in zero.items():
                self.check(doc.get('m1_sum') == 0 and doc.get('m1_count') == 0
                           and doc.get('m1_avg') is None
                           and key == f"{self.transform}#{doc.get('bucket_start_ms')}:{doc.get('device')}",
                           f'run {step}: invalid empty materialization {key}: {doc}')
            actual = {key: doc for key, doc in actual.items() if key not in zero}
            missing, extra = sorted(set(expected) - set(actual)), sorted(set(actual) - set(expected))
            differences = []
            for identifier in sorted(set(expected) & set(actual)):
                a, b = actual[identifier], expected[identifier]
                for field, value in b.items():
                    got = a.get(field)
                    equal = math.isclose(got, value, rel_tol=1e-9, abs_tol=1e-9) if (
                        field == 'm1_avg' and got is not None and value is not None) else got == value
                    if not equal:
                        differences.append({'id': identifier, 'field': field, 'expected': value, 'actual': got})
            dump(f'state-{step}.json', {'expected': expected, 'actual': actual, 'meta': current,
                                       'missing': missing, 'extra': extra, 'differences': differences, 'empty': zero})
            self.check(not (missing or extra or differences),
                       f'run {step}: target mismatch: {len(missing)} missing, {len(extra)} extra, '
                       f'{len(differences)} differing fields; see state-{step}.json')

    def dispatch(self, step, snapshot, mutate=False, inject=False, kill=False):
        if kill:
            self.invoke(step, 0, kill=True)
        changed = False
        for attempt in range(1, 65):
            # Snapshot belongs to the invocation which eventually returns success. A pending
            # attempt can finish an older finite plan before reconciling this newer snapshot.
            snapshot = self.e.checkpoint(self.source)
            rc = self.invoke(step, attempt, inject=inject and attempt == 1)
            if rc == 0:
                return snapshot
            if rc != 75:
                return snapshot
            if mutate and not changed:
                # Correct both already accepted and not-yet-accepted buckets, then advance the
                # selection while previous delivery is unfinished. This uses actual target
                # keys, independent of any worker's plan schema or ordering.
                accepted = set(self.gateway.accepted)
                buckets = {}
                for identifier, (doc, route) in self.documents.items():
                    if doc['ts'] <= self.now - HOUR:
                        key = f"{self.transform}#{doc['ts'] // HOUR * HOUR}:{doc['device']}"
                        buckets.setdefault(key, identifier)
                chosen = ([buckets[k] for k in sorted(accepted & buckets.keys())[:2]] +
                          [buckets[k] for k in sorted(buckets.keys() - accepted)[:2]])
                actions = []
                for identifier in chosen:
                    doc, route = self.documents[identifier]
                    actions += self.action(identifier, {**doc, 'm1': doc.get('m1', 0) + 1703}, route)
                if actions:
                    self.e.bulk(actions)
                self.ingest(100 + step, 20)
                changed = True
        self.check(False, f'run {step}: did not finish within the documented 64-invocation horizon')
        return self.e.checkpoint(self.source)

    def handoff(self, kind, step):
        """Abandon one invocation at an actual request boundary, then deliver it late."""
        self.ingest(step, 15)
        late = None
        if kind == 'target':
            # A late delivery creates a key with no target document in the earliest selected
            # hour; voiding it after the hold makes its correct state absent again.
            late = 'late-' + kind
            doc = {'ts': self.now - 49 * HOUR + 1, 'device': 'aux-late', 'm1': 23}
            self.e.bulk(self.action(late, doc, self.routes['0']))
            self.e.settle(self.source)
        abandoned = self.gateway
        abandoned.hold_kind = kind
        errors = []
        def run_old():
            try:
                self.invoke('abandoned-' + kind, 0)
            except BaseException as exc:
                errors.append(repr(exc))
        thread = threading.Thread(target=run_old, daemon=True)
        thread.start()
        deadline = time.monotonic() + 120
        while not abandoned.held.wait(timeout=.1):
            if not thread.is_alive() or time.monotonic() >= deadline:
                break
        if not abandoned.held.is_set():
            self.check(False, f'handoff {kind}: predecessor did not reach a deferred request')
            abandoned.release.set()
            thread.join(timeout=125)
            self.check(not errors, f'handoff {kind}: predecessor errors {errors}')
            abandoned.hold_kind = None
            return
        try:
            # Only the abandoned gateway is held. The replacement has its own request queue
            # and its own invocation credits, while both operate on the same actual indices.
            self.gateway = Gateway(self.e, self.source, self.target, self.meta, self.transform)
            old_ids = set((abandoned.held_ids or abandoned.accepted)[:8])
            chosen = {}
            for identifier, (doc, route) in self.documents.items():
                key = f"{self.transform}#{doc['ts'] // HOUR * HOUR}:{doc['device']}"
                if key in old_ids and doc['ts'] <= self.now - HOUR and identifier != late:
                    chosen.setdefault(key, identifier)
            actions = []
            for identifier in list(chosen.values())[:2]:
                doc, route = self.documents[identifier]
                actions += self.action(identifier, {**doc, 'm1': doc.get('m1', 0) + 9007}, route)
            self.check(bool(actions), f'handoff {kind}: no affected bucket available to correct')
            if kind == 'target' and chosen:
                key = sorted(chosen)[0]
                for identifier, (doc, route) in list(self.documents.items()):
                    actual_key = f"{self.transform}#{doc['ts']//HOUR*HOUR}:{doc['device']}"
                    if actual_key == key:
                        actions += self.action(identifier, {**doc, 'voided': True}, route)
                dump('handoff-revoked-key.json', {'key': key})
            if late:
                doc, route = self.documents[late]
                actions += self.action(late, {**doc, 'voided': True}, route)
            if actions:
                self.e.bulk(actions)
            # The target replacement keeps the abandoned invocation's clock, so a clock-derived
            # target version cannot order the late write.
            snapshot = self.ingest(step + 1000, 0 if kind == 'target' else 10)
            snapshot = self.dispatch('replacement-' + kind, snapshot)
            self.compare('before-release-' + kind, snapshot)
        finally:
            abandoned.release.set()
            self.check(abandoned.deferred_done.wait(timeout=120),
                       f'handoff {kind}: deferred request did not finish forwarding')
            thread.join(timeout=125)
            self.check(not thread.is_alive(), f'handoff {kind}: abandoned invocation did not terminate')
            self.check(not errors, f'handoff {kind}: predecessor errors {errors}')
            # Retain the actual deferred request and engine acknowledgements, not a worker schema.
            dump('handoff-' + kind + '.json', {'held_ids': abandoned.held_ids,
                 'accepted_before_and_after_release': abandoned.accepted, 'requests': abandoned.log})
            abandoned.close()
        self.compare('after-release-' + kind, snapshot)

    def run(self):
        self.create()
        self.seed()
        for step, minutes in enumerate([15, 15, 45, 5, 15, 120, 15, 15], 1):
            snapshot = self.ingest(step, minutes)
            snapshot = self.dispatch(step, snapshot, mutate=step in (2, 6),
                                     inject=step == 4, kill=step == 6)
            self.compare(step, snapshot, equality=step != 4)
        self.now += 20 * MINUTE
        snapshot = self.dispatch(9, self.e.checkpoint(self.source))
        self.compare(9, snapshot)
        snapshot = self.dispatch(10, self.e.checkpoint(self.source))
        self.compare(10, snapshot)
        # A sparse bucket holding one large integer metric, delivered at the same clock.
        large = {'ts': self.now - 3 * HOUR + 77, 'device': 'dev-large', 'm1': 2000000000}
        self.e.bulk(self.action('large-0', large, self.routes['1']))
        snapshot = self.dispatch('large-metric', self.e.settle(self.source))
        self.compare('large-metric', snapshot)
        # Ingestion overwrites that metric with a one-unit correction; the clock is unchanged.
        # Both integers are exact in the target's double field, so the sum must follow.
        self.e.bulk(self.action('large-0', {**large, 'm1': 2000000001}, self.routes['1']))
        snapshot = self.dispatch('unit-correction', self.e.settle(self.source))
        self.compare('unit-correction', snapshot)
        self.handoff('target', 11)
        self.handoff('metadata', 12)
        self.check(bool(self.injected), 'ingress schedule never reached a source search')
        self.check(bool(self.archived) and bool(self.revoked) and bool(self.restored),
                   'environment: archival/revocation/restore workload did not run')
        dump('selection-workload.json', {'archived': len(self.archived),
             'revoked': self.revoked, 'restored': self.restored})
        dump('summary.json', {'failures': self.failures, 'injected': self.injected,
                              'read_counts': [r['docs_read'] for r in self.runs],
                              'retry_exits': sum(r['rc'] == 75 for r in self.runs)})
        assert not self.failures, '\n'.join(self.failures)


def test_continuous_materialization():
    engine, domain, aws = provision()
    scenario = Scenario(engine)
    try:
        scenario.run()
    finally:
        if scenario.gateway:
            scenario.gateway.close()
        # Preserve evidence before disposing of this run's isolated domain.
        dump('failures.json', scenario.failures)
        aws.delete_domain(DomainName=domain)

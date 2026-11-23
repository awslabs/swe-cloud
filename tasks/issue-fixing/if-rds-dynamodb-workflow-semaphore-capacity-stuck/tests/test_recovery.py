import datetime
import json
import os
from pathlib import Path
import time
import uuid
import boto3
from cdk_seed.cfn_deploy import deploy
import psycopg
import pytest
from botocore.config import Config
from botocore.exceptions import ClientError
from observe import Monitor, change, scan
from rig import TCPProxy, WorkflowProxy, Pauses, launch


def wait(predicate, seconds):
    until = time.monotonic() + max(0, seconds)
    while time.monotonic() < until:
        if predicate():
            return True
        time.sleep(.15)
    return bool(predicate())


def test_controller_recovery():
    endpoint = os.environ['AWS_ENDPOINT_URL']
    suffix = uuid.uuid4().hex[:9]
    rds = boto3.client('rds', endpoint_url=endpoint, region_name='us-east-1',
                       config=Config(read_timeout=600, retries={'max_attempts': 1}))
    ddb = boto3.resource('dynamodb', endpoint_url=endpoint, region_name='us-east-1')
    name = 'capacity-' + suffix
    try:
        deploy(boto3.client('cloudformation', endpoint_url=endpoint, region_name='us-east-1'),
               'seed-' + name, Path(__file__).parent / 'cdk_seed/seed.template.json',
               InstanceName=name)
        status = {}
        def available():
            nonlocal status
            status = rds.describe_db_instances(DBInstanceIdentifier=name)['DBInstances'][0]
            return status['DBInstanceStatus'] == 'available'
        assert wait(available, 90), 'RDS did not become available'
        host, port = status['Endpoint']['Address'], status['Endpoint']['Port']
        direct = f'postgresql://syncadmin:local-password@{host}:{port}/sync'
        conn = None
        for _ in range(30):
            try:
                conn = psycopg.connect(direct, autocommit=True, connect_timeout=2)
                break
            except psycopg.OperationalError:
                time.sleep(.5)
        assert conn is not None, 'RDS endpoint unreachable'
        tables = {c: 'workflow-' + c + '-' + suffix for c in ('ctl-a', 'ctl-b')}
        ledger = 'execution-' + suffix
        for table, key in [(t, 'wf_key') for t in tables.values()] + [(ledger, 'exec_id')]:
            deploy(boto3.client('cloudformation', endpoint_url=endpoint, region_name='us-east-1'),
                   'seed-' + table, Path(__file__).parent / 'cdk_seed/table.template.json',
                   TableName=table, KeyName=key)
        # Independent DDL: never execute application-authored schema or import its clients.
        conn.execute('''CREATE TABLE sync_limit(name varchar(256), sizelimit int, PRIMARY KEY(name));
            CREATE TABLE sync_controller(controller varchar(64) NOT NULL, time timestamp, PRIMARY KEY(controller));
            CREATE TABLE sync_state(name varchar(256), workflowkey varchar(256), controller varchar(64) NOT NULL,
                held boolean, priority int, time timestamp, execution_id varchar(64) NOT NULL, PRIMARY KEY(name,workflowkey,controller));
            CREATE INDEX istate_name ON sync_state(name);
            CREATE INDEX istate_controller ON sync_state(controller);
            CREATE INDEX istate_workflowkey ON sync_state(workflowkey);
            CREATE INDEX istate_held ON sync_state(held);''')
    except Exception as exc:
        pytest.fail('environment: durable-service preflight failed: ' + repr(exc))

    sem = {x: 'batch/' + x + '-' + suffix for x in ('gate', 'pool', 'solo', 'crash', 'retry', 'launch', 'cancel', 'tree', 'queued-tree', 'active-tree', 'outage-tree')}
    limits = {sem['gate']: 1, sem['pool']: 2, sem['solo']: 1, sem['crash']: 1, sem['retry']: 1, sem['launch']: 1, sem['cancel']: 1}
    limits.update({sem[x]: 1 for x in ('tree','queued-tree','active-tree','outage-tree')})
    for lock, size in limits.items():
        conn.execute('INSERT INTO sync_limit VALUES (%s,%s)', (lock, size))
    keys = {x: 'batch/' + x + '-' + suffix for x in ('nightly', 'gate-job', 'b-wait', 'deleted', 'w2', 'w3',
                                                   'g2', 'long', 's2', 'k1', 'k2', 'k3', 's-b', 'retry', 'launch', 'launch-next', 'cancel', 'cancel-next', 'swap', 'tree', 'queued-tree', 'active-tree', 'tree-next', 'queued-tree-next', 'active-tree-next', 'normal-tree', 'outage-tree')}
    wf = {c: ddb.Table(t) for c,t in tables.items()}
    processes, proxies = [], []
    pauses = Pauses()
    monitor = None
    errors = []
    submissions = {}
    uids = {}
    def check(condition, message):
        if not condition:
            errors.append(message)
    def submit(controller, label, lock, duration, phase='Pending', uid=None, launch_delay=0):
        uid = uid or uuid.uuid4().hex
        uids[(controller,label)] = uid
        monitor.launch_delay[uid] = launch_delay
        item = {'wf_key': keys[label], 'uid': uid, 'phase': phase, 'semaphore': sem[lock], 'priority': 0,
                'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'run_seconds': duration, 'version': 1}
        if phase == 'Failed':
            item['message'] = 'Failed to acquire the synchronization lock. session proxy is closed'
        wf[controller].put_item(Item=item)
        submissions[(controller, label)] = time.monotonic()
        return item
    def row(lock, label, controller, held=True):
        return ('sem/' + sem[lock], keys[label], controller, held)
    def start(controller, label):
        return monitor.started(controller, keys[label], uids.get((controller,label)))
    def end(controller, label):
        return monitor.ended(controller, keys[label], uids.get((controller,label)))
    try:
        pg = {c: TCPProxy(host, port) for c in tables}
        api = {c: WorkflowProxy(endpoint, tables[c]) for c in tables}
        proxies.extend(pg.values())
        proxies.extend(api.values())
        monitor = Monitor(direct, endpoint, tables, ledger, limits)
        def run(controller, label):
            dsn = f'postgresql://syncadmin:local-password@127.0.0.1:{pg[controller].port}/sync'
            proc, ready, ok = launch(controller, controller, tables[controller], dsn,
                                     api[controller].endpoint, pauses, label)
            processes.append(proc)
            check(ok, label + ' did not become READY within 15 seconds')
            return proc, ready
        b, tb = run('ctl-b', 'west')
        scenario_end = tb + 300
        submit('ctl-b', 'nightly', 'pool', 200)
        submit('ctl-b', 'gate-job', 'gate', 200)
        submit('ctl-b', 'b-wait', 'gate', 10)
        initial = wait(lambda: start('ctl-b', 'nightly') and start('ctl-b', 'gate-job') and
                       row('gate', 'b-wait', 'ctl-b', False) in monitor.rows, 20)
        check(initial, 'initial foreign holders and waiter not admitted/enqueued')
        if initial:
            monitor.protected_held.update({row('pool', 'nightly', 'ctl-b')[:3], row('gate', 'gate-job', 'ctl-b')[:3]})
            monitor.protected_pending.add(row('gate', 'b-wait', 'ctl-b')[:3])
        submit('ctl-a', 'nightly', 'pool', 10, 'Failed')
        uids[('ctl-a','deleted')] = uuid.uuid4().hex
        for lock, label in [('pool', 'nightly'), ('solo', 'deleted')]:
            conn.execute("INSERT INTO sync_state VALUES (%s,%s,'ctl-a',true,0,CURRENT_TIMESTAMP - interval '1 hour',%s)",
                         ('sem/' + sem[lock], keys[label], uids[('ctl-a',label)]))
        seeded = {row('pool', 'nightly', 'ctl-a'), row('solo', 'deleted', 'ctl-a')}
        check(wait(lambda: seeded <= monitor.seen, 5), 'observer did not see the seeded SQL holders')
        a, ta = run('ctl-a', 'east')
        for label, lock, duration in [('w2','pool',10), ('w3','pool',10), ('g2','gate',10),
                                      ('long','solo',80), ('s2','solo',10)]:
            submit('ctl-a', label, lock, duration)
        check(wait(lambda: row('pool', 'nightly', 'ctl-a') not in monitor.rows and
                   row('solo', 'deleted', 'ctl-a') not in monitor.rows, ta + 20 - time.monotonic()),
              'startup orphan capacity was not recovered within 20 seconds')
        check(wait(lambda: start('ctl-a', 'w2') and start('ctl-a', 'long'), ta + 30 - time.monotonic()),
              'waiters did not receive recovered capacity within the startup handover bound')
        if start('ctl-a', 'long'):
            monitor.protected_held.add(row('solo', 'long', 'ctl-a')[:3])
        # Dynamic DAG payloads share one workflow reservation. Root payload
        # completion is not tree completion; queued descendants retain their
        # own launch interlocks, including under a still-running ancestor.
        tree_specs = {
            'tree': [
                {'tag':'branch','parent':'root','register':0,'deliver':0,'duration':2},
                {'tag':'leaf','parent':'branch','register':0,'deliver':0,'duration':55},
                {'tag':'sibling','parent':'root','register':0,'deliver':0,'duration':45},
                {'tag':'queued','parent':'leaf','register':0,'deliver':38,'duration':20},
                {'tag':'late','parent':'branch','register':28,'deliver':28,'duration':10}],
            'queued-tree': [
                {'tag':'branch','parent':'root','register':0,'deliver':0,'duration':2},
                {'tag':'queued','parent':'branch','register':0,'deliver':38,'duration':20},
                {'tag':'late','parent':'branch','register':28,'deliver':28,'duration':10}],
            'active-tree': [
                {'tag':'branch','parent':'root','register':0,'deliver':0,'duration':2},
                {'tag':'queued','parent':'branch','register':0,'deliver':38,'duration':20},
                {'tag':'late','parent':'root','register':28,'deliver':28,'duration':10}]}
        trees = {}
        for label, specs in tree_specs.items():
            uid = uuid.uuid4().hex
            nodes = monitor.branches.plan(uid, specs)
            original_tree = submit('ctl-a', label, label, 55 if label == 'active-tree' else 2, uid=uid)
            trees[label] = {'uid':uid, 'nodes':nodes, 'original':original_tree}
        def branches_ready():
            return all(start('ctl-a', label) and all(
                monitor.branches.nodes.get(t['nodes'][spec['tag']], {}).get('registered')
                for spec in tree_specs[label] if spec['tag'] != 'late')
                and (label == 'active-tree' or end('ctl-a',label))
                for label,t in trees.items())
        check(wait(branches_ready, 15), 'branch execution workload was not published')
        # A retried child publication wins immediately before the first
        # mutation of this root receipt. This is a transport-level interleave,
        # not a demand for one particular read or close implementation.
        racing_tree = trees['queued-tree']
        racing_uid = racing_tree['uid']
        racing_node = uuid.uuid4().hex
        racing_spec = {'tag':'racing', 'node':racing_node, 'parent':racing_uid,
                       'register':0, 'deliver':38, 'duration':20,
                       'attempted':True, 'registered':False, 'delivered':False, 'start':None, 'end':None}
        monitor.branches.plans[racing_uid].append(racing_spec)
        def publish_during_close(writes):
            if not any(key.get('wf_key', {}).get('S') == '@run/' + racing_uid for key in writes):
                return
            api['ctl-a'].before_write = None
            try:
                wf['ctl-a'].update_item(Key={'wf_key':'@run/' + racing_uid},
                    UpdateExpression='ADD #children :child',
                    ConditionExpression='(attribute_not_exists(#closed) OR #closed=:no) AND #started=:yes',
                    ExpressionAttributeNames={'#children':'children', '#closed':'closed', '#started':'started'},
                    ExpressionAttributeValues={':child':{racing_node}, ':no':False, ':yes':True})
                racing_spec['registered'] = True
            except ClientError as exc:
                if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    raise
        api['ctl-a'].before_write = publish_during_close
        # One reused name, one deleted DAG and one Failed DAG whose root is
        # still running. All are ordinary client lifecycle operations.
        for label, tree in trees.items():
            if label == 'active-tree':
                current = wf['ctl-a'].get_item(Key={'wf_key':keys[label]}, ConsistentRead=True)['Item']
                change(wf['ctl-a'], current, {'phase':'Failed'})
            else:
                wf['ctl-a'].delete_item(Key={'wf_key':keys[label]})
            tree['cancelled_at'] = time.monotonic()
            successor = label if label == 'tree' else label + '-next'
            tree['successor'] = successor
            submit('ctl-a', successor, label, 10)
        normal_uid = uuid.uuid4().hex
        monitor.branches.plan(normal_uid, [
            {'tag':'worker','parent':'root','register':0,'deliver':0,'duration':14},
            {'tag':'child','parent':'worker','register':5,'deliver':5,'duration':5}])
        submit('ctl-a','normal-tree','tree',2,uid=normal_uid)
        # Receipts describe execution, while API deletion/recreation describes intent.
        # Reuse a name while the original pod is running, and cancel another pod
        # without removing its API object. Both still consume capacity.
        original = submit('ctl-a', 'retry', 'retry', 55)
        cancelled = submit('ctl-a', 'cancel', 'cancel', 45)
        delayed = submit('ctl-a', 'launch', 'launch', 20, launch_delay=30)
        check(wait(lambda: start('ctl-a','retry') and start('ctl-a','cancel') and
                   ('ctl-a', keys['launch'], delayed['uid']) in monitor.deliveries, 15),
              'executor did not receive cancellation workload')
        wf['ctl-a'].delete_item(Key={'wf_key': keys['retry']})
        replacement = submit('ctl-a', 'retry', 'retry', 10)
        current = wf['ctl-a'].get_item(Key={'wf_key': keys['cancel']}, ConsistentRead=True)['Item']
        change(wf['ctl-a'], current, {'phase': 'Failed'})
        submit('ctl-a', 'cancel-next', 'cancel', 10)
        wf['ctl-a'].delete_item(Key={'wf_key': keys['launch']})
        deleted_at = time.monotonic()
        submit('ctl-a', 'launch-next', 'launch', 10)
        check(wait(lambda: row('launch','launch','ctl-a') not in monitor.rows,
                   deleted_at + 20 - time.monotonic()),
              'unused launch reservation was not recovered within 20 seconds')
        # Release capacity while the queued delivery is still outstanding.
        check(wait(lambda: start('ctl-a','launch-next'), deleted_at + 30 - time.monotonic()),
              'launch successor did not receive cancelled unused capacity')
        crash_at = (start('ctl-a', 'long') + 20) if start('ctl-a', 'long') else ta + 30
        wait(lambda: time.monotonic() >= crash_at, max(0, crash_at-time.monotonic()) + .5)
        pauses.target = keys['k1']
        submit('ctl-a', 'k1', 'crash', 10)
        announced = wait(lambda: pauses.arrived.is_set() and row('crash', 'k1', 'ctl-a') in monitor.rows, 10)
        check(announced, 'admission pause did not announce a committed hold')
        a.kill()
        a.wait(timeout=5)
        killed = time.monotonic()
        pauses.release.set()
        item = wf['ctl-a'].get_item(Key={'wf_key': keys['k1']}, ConsistentRead=True)['Item']
        check('sync_holding' not in item, 'workflow status landed before the admission interruption')
        if 'sync_holding' not in item:
            change(wf['ctl-a'], item, {'phase': 'Failed'})
        submit('ctl-a', 'k2', 'crash', 10)
        submit('ctl-b', 's-b', 'solo', 10)
        wait(lambda: time.monotonic() >= killed + 30, 30.5)
        a2, ta2 = run('ctl-a', 'east-restarted')
        # Reclaim and network schedules overlap: the restart must recover durable rows, not just status.
        wedge_start = ta2 + 10
        # The other cluster admits a DAG whose child launch request stays queued.
        # Its workflow is cancelled through the API once only SQL is interrupted.
        outage_uid = uuid.uuid4().hex
        monitor.branches.plan(outage_uid, [
            {'tag':'queued','parent':'root','register':0,'deliver':10**6,'duration':5}])
        outage_child = monitor.branches.plans[outage_uid][0]
        submit('ctl-b', 'outage-tree', 'outage-tree', 2, uid=outage_uid)
        check(wait(lambda: end('ctl-b','outage-tree') and outage_child['registered'], 15),
              'database-interruption cancellation workload was not published')
        wait(lambda: time.monotonic() >= wedge_start, max(0, wedge_start-time.monotonic()) + .5)
        pg['ctl-b'].set_cut(True)
        cut = time.monotonic()
        current = wf['ctl-b'].get_item(Key={'wf_key': keys['outage-tree']}, ConsistentRead=True).get('Item')
        outage_cancelled = bool(current and current['uid'] == outage_uid and current['phase'] == 'Running'
                                and start('ctl-b','outage-tree') and outage_child['registered'])
        if outage_cancelled:
            change(wf['ctl-b'], current, {'phase': 'Failed'})
            # The queued request arrives 30 seconds after cancellation, past the
            # 20-second launch-disable bound, while SQL is still unreachable.
            outage_child['deliver'] = time.monotonic() + 30 - start('ctl-b','outage-tree')
        check(wait(lambda: row('crash', 'k1', 'ctl-a') not in monitor.rows,
                   ta2 + 20 - time.monotonic()), 'interrupted admission leaked its held row after restart')
        check(wait(lambda: start('ctl-a', 'k2'), ta2 + 30 - time.monotonic()),
              'crash-pool waiter did not receive recovered capacity')
        wait(lambda: time.monotonic() >= cut + 40, max(0, cut+40-time.monotonic()) + .5)
        prior_beat = monitor.heartbeats.get('ctl-b')
        pg['ctl-b'].set_cut(False)
        restored = time.monotonic()
        check(wait(lambda: monitor.heartbeats.get('ctl-b') is not None and
                   (prior_beat is None or monitor.heartbeats['ctl-b'] > prior_beat), 20),
              'same controller did not resume database heartbeats after connectivity returned')
        check(b.poll() is None, 'controller exited during the network interruption')
        check(wait(lambda: end('ctl-a', 'k2'), 15), 'first crash-pool handover never completed')
        pauses.release.clear()
        pauses.arrived.clear()
        pauses.target = keys['k3']
        submit('ctl-a', 'k3', 'crash', 10)
        held_pending = wait(lambda: pauses.arrived.is_set() and row('crash', 'k3', 'ctl-a') in monitor.rows, 10)
        check(held_pending, 'second admission did not reach its committed pause')
        if held_pending:
            monitor.protected_held.add(row('crash', 'k3', 'ctl-a')[:3])
        window = time.monotonic() + 10.5
        wait(lambda: time.monotonic() >= window, 11)
        check(row('crash', 'k3', 'ctl-a') in monitor.rows,
              'in-flight admission lost its hold before workflow status was persisted')
        pauses.release.set()
        check(wait(lambda: end('ctl-a','k3'), 20), 'live admission did not finish after its pause')
        pauses.release.clear()
        pauses.arrived.clear()
        pauses.target = keys['swap']
        stale_admission = submit('ctl-a', 'swap', 'crash', 10)
        check(wait(lambda: pauses.arrived.is_set() and row('crash','swap','ctl-a') in monitor.rows, 10),
              'replaceable admission did not reach its commit pause')
        wf['ctl-a'].delete_item(Key={'wf_key': keys['swap']})
        new_admission = submit('ctl-a', 'swap', 'crash', 10)
        old_token_row = (*row('crash','swap','ctl-a'), stale_admission['uid'])
        check(wait(lambda: old_token_row not in monitor.token_rows, 20),
              'recreated Pending workflow prevented recovery of its predecessor reservation')
        pauses.release.set()
        check(wait(lambda: start('ctl-a','swap'), 20),
              'replacement admission did not start after a stale status write resumed')
        check(monitor.started('ctl-a',keys['swap'],stale_admission['uid']) is None,
              'deleted admission incarnation executed')
        check(monitor.ended('ctl-a', keys['retry'], original['uid']) is not None,
              'deleted incarnation never finished execution')
        check(monitor.started('ctl-a', keys['launch'], delayed['uid']) is None,
              'a cancelled queued request started after its reservation was recovered')
        new_start = monitor.started('ctl-a', keys['retry'], replacement['uid'])
        old_end = monitor.ended('ctl-a', keys['retry'], original['uid'])
        check(new_start is not None and old_end is not None and new_start >= old_end,
              'replacement inherited or overtook its predecessor reservation')
        if end('ctl-a','cancel') and start('ctl-a','cancel-next'):
            check(start('ctl-a','cancel-next') >= end('ctl-a','cancel'),
                  'terminal workflow released capacity before its execution stopped')
        check(all(spec['start'] is not None and spec['end'] is not None
                  for spec in monitor.branches.plans[normal_uid]),
              'live DAG lost a payload or expansion after its root finished')
        check(racing_spec['registered'], 'concurrent child publication did not reach its durable parent')
        check(outage_cancelled and outage_child['delivered'],
              'database-interruption cancellation schedule did not run')
        check(outage_child['start'] is None,
              'cancelled descendant launched during a database interruption, after the launch-disable deadline')
        for label, tree in trees.items():
            uid = tree['uid']
            specs = monitor.branches.plans[uid]
            for spec in specs:
                if spec['tag'] in {'queued','late','racing'}:
                    check(spec['start'] is None,
                          'cancelled descendant launched after finalization deadline: ' + label + ':' + spec['tag'])
                if spec['tag'] == 'late':
                    check(spec['node'] in monitor.branches.registration_denied,
                          'cancelled subtree still accepted late expansion: ' + label)
            root_execution = monitor.executions.get(('ctl-a',keys[label],uid), {})
            ends = [root_execution.get('end')] + [s['end'] for s in specs if s['start'] is not None]
            if all(t is not None for t in ends):
                usable = max(tree['cancelled_at'], *ends)
                successor_start = start('ctl-a',tree['successor'])
                check(successor_start is not None and usable <= successor_start <= usable + 30,
                      'branch capacity did not drain and hand over safely: ' + label)
                if label == 'queued-tree':
                    removed = monitor.removed.get(row(label,label,'ctl-a'))
                    check(removed is not None and removed <= tree['cancelled_at'] + 20,
                          'unused descendant tree reservation was not recovered within 20 seconds')
            else:
                check(False, 'cancelled tree did not finish its started payloads: ' + label)
        expected = [('ctl-b', x) for x in ('nightly', 'gate-job', 'b-wait', 's-b')] + [
                    ('ctl-a', x) for x in ('w2','w3','g2','long','s2','k2','k3','retry','launch-next','cancel-next','swap','tree','queued-tree-next','active-tree-next','normal-tree')]
        check(wait(lambda: all(monitor.items[c].get(keys[label], {}).get('phase') == 'Succeeded' for c,label in expected),
                   scenario_end - time.monotonic()), 'workload failed to drain within 300 seconds')
        check(wait(lambda: not monitor.rows, 10), 'terminal or absent workflows left synchronization rows')
        for c, label in expected:
            check(start(c, label) is not None and end(c, label) is not None, f'no complete execution for {c}:{label}')
        for lock, orphan, waiter in [('pool','nightly','w2'), ('solo','deleted','long'), ('crash','k1','k2')]:
            released = monitor.removed.get(row(lock, orphan, 'ctl-a'))
            if released is not None and start('ctl-a', waiter) is not None:
                available = max(released, submissions[('ctl-a', waiter)])
                check(start('ctl-a', waiter) <= available + 10,
                      'reclaimed capacity did not reach the front waiter within 10 seconds: ' + lock)
        if end('ctl-b', 'gate-job') and start('ctl-b','b-wait'):
            check(0 <= start('ctl-b','b-wait') - end('ctl-b','gate-job') <= 10,
                  'gate did not hand over to the earlier foreign waiter')
        if end('ctl-b','b-wait') and start('ctl-a','g2'):
            check(0 <= start('ctl-a','g2') - end('ctl-b','b-wait') <= 10,
                  'gate did not hand over to the local waiter after the foreign waiter')
        if end('ctl-a', 'long') and start('ctl-a','s2'):
            check(0 <= start('ctl-a','s2') - end('ctl-a','long') <= 10,
                  'solo did not hand over in queue order after controller restart')
        check(row('crash','k1','ctl-a') in monitor.seen, 'no SQL commit was observed for interrupted admission')
        assert not errors and not monitor.violations, json.dumps({'checks': errors, 'observations': sorted(monitor.violations)}, indent=2)
    finally:
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
        for proc in processes:
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
                proc.wait(timeout=5)
        if monitor:
            monitor.close()
        pauses.close()
        for proxy in proxies:
            proxy.close()
        conn.close()
        Path('/logs/verifier/checks.json').write_text(json.dumps(errors, indent=2))

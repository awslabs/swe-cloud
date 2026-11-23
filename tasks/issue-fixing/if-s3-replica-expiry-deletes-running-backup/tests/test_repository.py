"""Outcome verifier for shared S3 repository maintenance."""
import datetime as dt
import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import threading
import uuid

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
import pytest
from relay import Relay
from cdk_seed.cfn_deploy import deploy

ENDPOINT = os.environ.get('EMULATOR_ENDPOINT', 'http://localhost:4566')
APP = Path(os.environ.get('APP_UNDER_TEST', '/app'))
EVIDENCE = Path(os.environ.get('VERIFIER_EVIDENCE_DIR', '/logs/verifier/cases'))
EVIDENCE.mkdir(parents=True, exist_ok=True)


def client():
    return boto3.client('s3', endpoint_url=ENDPOINT, region_name='us-east-1',
                        aws_access_key_id='test', aws_secret_access_key='test',
                        config=Config(s3={'addressing_style': 'path'},
                                      retries={'max_attempts': 2}))


def encode(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':')).encode()


def objects(s3, bucket, prefix='backup/'):
    result = {}
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get('Contents', []):
            result[obj['Key']] = obj['ETag']
    return result


def read_info(s3, bucket):
    response = s3.get_object(Bucket=bucket, Key='backup/backup.info')
    info = json.loads(response['Body'].read())
    response['Body'].close()
    return info


def settings(s3, bucket):
    versioning = s3.get_bucket_versioning(Bucket=bucket).get('Status')
    try:
        lifecycle = s3.get_bucket_lifecycle_configuration(Bucket=bucket).get('Rules')
    except ClientError as exc:
        lifecycle = exc.response['Error']['Code']
    return versioning, lifecycle


@pytest.fixture(scope='session', autouse=True)
def repository_service_available():
    s3 = client()
    bucket = 'preflight-' + uuid.uuid4().hex[:16]
    contender = None
    try:
        s3.create_bucket(Bucket=bucket)
        old = s3.put_object(Bucket=bucket, Key='probe', Body=b'one')['ETag']
        new = s3.put_object(Bucket=bucket, Key='probe', Body=b'two', IfMatch=old)['ETag']
        contender = client()
        try:
            contender.put_object(Bucket=bucket, Key='probe', Body=b'three', IfMatch=old)
        except ClientError as exc:
            assert exc.response['ResponseMetadata']['HTTPStatusCode'] == 412
        else:
            raise AssertionError('stale conditional PUT was accepted')
        contender.close()
        contender = None
        result = s3.get_object(Bucket=bucket, Key='probe')
        assert result['Body'].read() == b'two'
        result['Body'].close()
        assert s3.head_object(Bucket=bucket, Key='probe')['ETag'] == new
        assert objects(s3, bucket, prefix='') == {'probe': new}
    except Exception as exc:
        pytest.fail('environment: repository capability preflight: ' + repr(exc))
    finally:
        if contender:
            contender.close()
        try:
            s3.delete_object(Bucket=bucket, Key='probe')
            s3.delete_bucket(Bucket=bucket)
        except Exception:
            pass
        s3.close()


class Primary:
    def __init__(self, bucket, label, files, directory):
        self.ledger_path = directory / 'ledger.json'
        self.stderr = open(directory / 'primary.stderr', 'w')
        self.process = subprocess.Popen(
            [sys.executable, '-I', str(Path(__file__).with_name('primary.py')),
             ENDPOINT, bucket, label, str(files), str(self.ledger_path)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
            cwd=tempfile.mkdtemp(prefix='primary-'),
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': '/app'})
        self.phase = 0
        self.lock = threading.Lock()

    def advance(self, target):
        with self.lock:
            if self.phase >= target:
                return
            self.process.stdin.write(f'{target}\n'.encode())
            self.process.stdin.flush()
            ready, _, _ = select.select([self.process.stdout], [], [], 20)
            if not ready:
                raise RuntimeError('environment: primary did not announce completion')
            response = self.process.stdout.readline()
            if not response:
                raise RuntimeError('environment: primary exited: ' + str(self.ledger_path.parent))
            assert json.loads(response)['at'] == target
            self.phase = target

    def ledger(self):
        return json.loads(self.ledger_path.read_text())

    def kill(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.stderr.close()


def seed(s3, bucket, variant, no_writer=False):
    retention = 3 + variant % 2
    base = dt.datetime(2021 + variant % 4, 2 + variant % 3, 7 + variant % 9)
    label = lambda minutes: (base + dt.timedelta(minutes=minutes)).strftime('%Y%m%d-%H%M%SF')
    fulls = [label(i * 60) for i in range(retention + 1)]
    orphan1, orphan2 = label(25), label(85)
    writer_label = label((retention + 1) * 60 + 10)
    files = 6 + 2 * (variant % 3)
    info = {'format': 1, 'current': {}}
    ledger = {}
    deploy(boto3.client('cloudformation', endpoint_url=ENDPOINT, region_name='us-east-1',
                        aws_access_key_id='test', aws_secret_access_key='test'),
           'seed-' + bucket, Path(__file__).parent / 'cdk_seed/seed.template.json',
           BucketName=bucket)

    def put(lab, suffix, body):
        key = f'backup/{lab}/{suffix}'
        etag = s3.put_object(Bucket=bucket, Key=key, Body=body)['ETag']
        ledger.setdefault(lab, {})[key] = etag

    for lab in fulls:
        manifest = {'label': lab, 'type': 'full', 'prior': None, 'files': files}
        info['current'][lab] = {'type': 'full', 'prior': None, 'files': files}
        put(lab, 'backup.manifest.copy', encode(manifest))
        put(lab, 'backup.manifest', encode(manifest))
        for index in range(files):
            put(lab, f'pg_data/{index:04d}', os.urandom(96))
    put(orphan1, 'backup.manifest.copy', encode({'label': orphan1, 'copied': 5}))
    for index in range(5):
        put(orphan1, f'pg_data/{index:04d}', os.urandom(64))
    for index in range(3):
        put(orphan2, f'pg_data/{index:04d}', os.urandom(64))
    orphans = [orphan1, orphan2]
    if no_writer:
        for index in range(3):
            put(writer_label, f'pg_data/{index:04d}', os.urandom(64))
        orphans.append(writer_label)
    s3.put_object(Bucket=bucket, Key='backup/backup.info', Body=encode(info))
    # Belongs to another tool, outside label prefixes.
    control_key = 'backup/operator-notes.txt'
    control_body = os.urandom(37)
    s3.put_object(Bucket=bucket, Key=control_key, Body=control_body)
    return fulls, orphans, writer_label, files, retention, ledger, (control_key, control_body)


def run_case(name, variant=0, phase=2, target=None, hold=None, crashed=False, no_writer=False):
    directory = EVIDENCE / (name + '-' + uuid.uuid4().hex[:6])
    directory.mkdir()
    s3 = client()
    bucket = 'repo-' + uuid.uuid4().hex[:18]
    primary = relay = None
    try:
        fulls, orphans, writer_label, files, retention, seed_ledger, control = seed(
            s3, bucket, variant, no_writer)
        original_settings = settings(s3, bucket)
        initial = None
        if not no_writer:
            primary = Primary(bucket, writer_label, files, directory)
            primary.advance(phase)
            initial = primary.ledger()
            if crashed:
                primary.kill()
        # Migrated failed work can have newer object timestamps than a parked
        # primary. Re-materialize its debris after that primary is paused.
        for orphan in orphans:
            for key in objects(s3, bucket, f'backup/{orphan}/'):
                s3.put_object(Bucket=bucket, Key=key, Body=os.urandom(73))
        held = False

        def before(index):
            nonlocal held
            if hold == index and not held:
                held = True
                primary.advance(target)

        relay = Relay(ENDPOINT, bucket, writer_label, before=before)
        try:
            result = subprocess.run(
                [sys.executable, str(APP / 'repoctl.py'), 'expire', '--bucket', bucket,
                 '--retention-full', str(retention)], cwd=tempfile.mkdtemp(prefix='standby-'),
                env={**os.environ, 'AWS_ENDPOINT_URL': relay.url, 'PYTHONPATH': str(APP),
                     'PYTHONDONTWRITEBYTECODE': '1'},
                capture_output=True, timeout=30)
        except subprocess.TimeoutExpired as exc:
            (directory / 'expire.stderr').write_bytes(exc.stderr or b'')
            pytest.fail('maintenance did not finish while the primary was parked')
        (directory / 'expire.stdout').write_bytes(result.stdout)
        (directory / 'expire.stderr').write_bytes(result.stderr)
        (directory / 'requests.json').write_text(json.dumps(relay.events, indent=2))
        errors = []
        if result.returncode:
            errors.append(f'expire exit {result.returncode}; see {directory}')
        if relay.errors:
            errors.append('environment: relay failures ' + repr(relay.errors))
        if relay.touched and not no_writer:
            errors.append('maintenance modified primary objects: ' + repr(relay.touched))
        # Observe parked state BEFORE allowing its remaining writes to mask damage.
        if primary and not held and phase < 3:
            observed = objects(s3, bucket, f'backup/{writer_label}/')
            if observed != initial:
                errors.append('parked primary objects changed before completion')
        if not held:
            before_info = read_info(s3, bucket)
            complete = fulls + ([writer_label] if primary and phase >= 3 else [])
            expected_current = set(sorted(complete, reverse=True)[:retention])
            if set(before_info['current']) != expected_current:
                errors.append('uncontended maintenance did not retain the newest requested count of complete backups')
        if primary and not crashed:
            primary.advance(4)
        expected_writer = primary.ledger() if primary else None
        final = objects(s3, bucket)
        info = read_info(s3, bucket)
        current = info.get('current', {})
        (directory / 'final.json').write_text(json.dumps({'info': info, 'keys': final}, indent=2))

        def label_objects(lab):
            return {key: etag for key, etag in final.items() if key.startswith(f'backup/{lab}/')}

        if primary:
            if label_objects(writer_label) != expected_writer:
                errors.append('primary key/ETag ledger differs from current S3 objects')
            if not crashed and writer_label not in current:
                errors.append('primary catalog registration lost')
            if crashed and writer_label in current:
                errors.append('incomplete backup registered as complete')
        if fulls[0] in current or label_objects(fulls[0]):
            errors.append('oldest full was not reclaimed by retention')
        if fulls[1] in current:
            if label_objects(fulls[1]) != seed_ledger[fulls[1]]:
                errors.append('retained boundary backup damaged')
        elif label_objects(fulls[1]):
            errors.append('expired boundary backup leaked storage')
        for lab in fulls[2:]:
            if lab not in current or label_objects(lab) != seed_ledger[lab]:
                errors.append('retained backup missing or altered: ' + lab)
        for lab in orphans:
            if label_objects(lab) or lab in current:
                errors.append('failed-backup storage not reclaimed: ' + lab)
        for lab in current:
            if f'backup/{lab}/backup.manifest' not in final:
                errors.append('catalog points to missing final manifest: ' + lab)
        if settings(s3, bucket) != original_settings:
            errors.append('bucket configuration changed')
        response = s3.get_object(Bucket=bucket, Key=control[0])
        if response['Body'].read() != control[1]:
            errors.append('object outside backup label prefixes changed')
        response['Body'].close()
        assert not errors, '\n'.join(errors)
        return len(relay.events)
    finally:
        if primary:
            primary.kill()
        if relay:
            relay.close()
        # Keep case evidence on disk, not a growing collection of live buckets.
        try:
            for key in objects(s3, bucket, prefix=''):
                s3.delete_object(Bucket=bucket, Key=key)
            s3.delete_bucket(Bucket=bucket)
        except ClientError:
            pass
        s3.close()


@pytest.mark.parametrize('target', [3, 4])
def test_shared_repository(target):
    # Measure the implementation's own ordinary request sequence, then place the
    # primary's publication before each of those requests. No program line or
    # particular S3 operation is assumed by the schedule.
    baseline = run_case(f'baseline-{target}', variant=target, phase=2)
    failures = []
    for index in range(1, min(baseline + 1, 200) + 1):
        try:
            run_case(f'run-{target}-{index}', variant=target, phase=2,
                     target=target, hold=index)
        except Exception as exc:
            failures.append(f'request {index}: {exc}')
    assert not failures, '\n\n'.join(failures)


@pytest.mark.parametrize('phase', [1, 2, 3, 4])
def test_repository_maintenance(phase):
    run_case(f'maintenance-{phase}', variant=phase, phase=phase)


def test_interrupted_primary():
    run_case('interrupted', variant=5, phase=2, crashed=True)


def test_storage_reclamation():
    run_case('storage', variant=6, no_writer=True)


# A recovery run's policy may keep every surviving backup. A previous published
# retirement still has to finish; its leftover manifest is not an import.
def recovery_case(name, cut=None, rival_at=None, recovery='reconcile', recovery_cut=None):
    directory = EVIDENCE / (name + '-' + uuid.uuid4().hex[:6])
    directory.mkdir()
    s3 = client()
    bucket = 'repo-' + uuid.uuid4().hex[:18]
    primary = relay = process = None
    try:
        fulls, orphans, writer_label, files, _, ledger, control = seed(s3, bucket, 8)
        # The interrupted policy retires several backups, so partial deletion
        # can leave both markerless work and complete retired manifests.
        count = 2
        protected = set(fulls[-(count - 1 if rival_at else count):])
        original_settings = settings(s3, bucket)
        primary = Primary(bucket, writer_label, files, directory)
        primary.advance(2)
        parked = primary.ledger()
        retired = set()
        audits = []

        def observe_retirements():
            inventory = read_info(s3, bucket)['current']
            state = objects(s3, bucket)
            retired.update(set(fulls) - set(inventory))
            for lab in fulls:
                existing = {k: v for k, v in state.items() if k.startswith(f'backup/{lab}/')}
                if existing != ledger[lab]:
                    retired.add(lab)
            return inventory, state

        def audit_inventory(context):
            inventory, state = observe_retirements()
            for lab in fulls:
                existing = {k: v for k, v in state.items() if k.startswith(f'backup/{lab}/')}
                if lab in inventory and existing != ledger[lab]:
                    audits.append(context + ': restore inventory advertises damaged backup ' + lab)
                if lab in protected and (lab not in inventory or existing != ledger[lab]):
                    audits.append(context + ': newest retained backup damaged ' + lab)
            for lab in retired:
                if lab in inventory:
                    audits.append(context + ': retired backup reappeared ' + lab)
            existing = {k: v for k, v in state.items() if k.startswith(f'backup/{writer_label}/')}
            if existing != (primary.ledger() if primary.phase >= 3 else parked):
                audits.append(context + ': primary bytes changed')

        recovery_requests = 0

        def run_maintenance(tag, command, keep, endpoint, cut_at=None):
            nonlocal recovery_requests
            args = [sys.executable, str(APP / 'repoctl.py'), command, '--bucket', bucket]
            if command == 'expire':
                args += ['--retention-full', str(keep)]
            killed = threading.Event()
            transport = worker = None

            def interrupt(index):
                if index == cut_at:
                    killed.set()
                    transport.stopped.set()
                    worker.kill()
                    worker.wait(timeout=5)
                    return False
                return True

            try:
                if cut_at is not None or tag == 'cold-recovery':
                    transport = Relay(ENDPOINT, bucket, writer_label, after=interrupt)
                    endpoint = transport.url
                worker = subprocess.Popen(args, cwd=tempfile.mkdtemp(prefix='replacement-'),
                                          env={**os.environ, 'AWS_ENDPOINT_URL': endpoint,
                                               'PYTHONPATH': str(APP), 'PYTHONDONTWRITEBYTECODE': '1'},
                                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                stdout, stderr = worker.communicate(timeout=30)
                (directory / (tag + '.stdout')).write_bytes(stdout)
                (directory / (tag + '.stderr')).write_bytes(stderr)
                if transport:
                    transport.close()
                    recovery_requests = len(transport.events)
                    (directory / (tag + '.requests.json')).write_text(json.dumps(transport.events, indent=2))
                    assert not transport.touched, 'recovery modified primary objects'
                    assert not transport.errors, 'recovery transport errors: ' + repr(transport.errors)
                    transport = None
                assert killed.is_set() or worker.returncode == 0, f'{tag}: exit {worker.returncode}; {directory}'
                return killed.is_set()
            finally:
                if worker and worker.poll() is None:
                    worker.kill()
                    worker.communicate(timeout=5)
                if transport:
                    transport.close()

        rival_ran = False

        def before(index):
            nonlocal rival_ran
            if rival_at != index or rival_ran:
                return
            rival_ran = True
            # Record decisions before a competing recovery can mask them.
            observe_retirements()
            primary.advance(3)
            # An independent process recovers prior work under a larger policy.
            # It must neither undo the old decision nor infer that a new primary
            # completion is garbage. Original worker resumes after it returns.
            run_maintenance('rival', recovery, len(fulls) + 2, ENDPOINT)
            audit_inventory('replacement returned')

        stopped = threading.Event()

        def after(index):
            if cut == 'retirement':
                event = relay.events[index - 1]
                if event['method'] not in ('PUT', 'DELETE', 'POST') or event['status'] >= 300:
                    return True
                inventory, state = observe_retirements()
                if not any(lab in retired and any(k.startswith(f'backup/{lab}/') for k in state)
                           for lab in fulls):
                    return True
            elif cut != index:
                return True
            stopped.set()
            relay.stopped.set()
            process.kill()
            process.wait(timeout=5)
            return False

        relay = Relay(ENDPOINT, bucket, writer_label, before=before, after=after)
        args = [sys.executable, str(APP / 'repoctl.py'), 'expire', '--bucket', bucket,
                '--retention-full', str(count)]
        process = subprocess.Popen(args, cwd=tempfile.mkdtemp(prefix='old-standby-'),
                                   env={**os.environ, 'AWS_ENDPOINT_URL': relay.url,
                                        'PYTHONPATH': str(APP), 'PYTHONDONTWRITEBYTECODE': '1'},
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            stdout, stderr = process.communicate(timeout=60 if rival_at else 30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            pytest.fail('maintenance failed to finish after a replacement ran')
        relay.close()
        request_count = len(relay.events)
        (directory / 'old.stdout').write_bytes(stdout)
        (directory / 'old.stderr').write_bytes(stderr)
        (directory / 'requests.json').write_text(json.dumps(relay.events, indent=2))
        assert not relay.errors, 'environment: relay failures ' + repr(relay.errors)
        assert not relay.touched, 'maintenance modified primary objects: ' + repr(relay.touched)
        relay = None
        if not stopped.is_set():
            assert process.returncode == 0, f'old worker exit {process.returncode}; {directory}'
        # A crash may have happened before any retirement was published; that
        # is legitimate, and recovery must keep those never-retired fulls.
        observe_retirements()
        assert not (retired & protected), 'retirement selected a newest retained backup'
        primary.advance(3)
        expected = (set(fulls) - retired) | {writer_label}
        killed_again = run_maintenance('cold-recovery', recovery, len(fulls) + 2,
                                       ENDPOINT, cut_at=recovery_cut)
        if killed_again:
            run_maintenance('second-recovery', recovery, len(fulls) + 2, ENDPOINT)
        audit_inventory('cold recovery returned')
        primary.advance(4)
        final = objects(s3, bucket)
        info = read_info(s3, bucket)
        (directory / 'final.json').write_text(json.dumps(
            {'info': info, 'keys': final, 'retired': sorted(retired), 'expected': sorted(expected)}, indent=2))
        assert set(info['current']) == expected, 'recovery lost a live backup or resurrected a retirement'
        for lab in fulls:
            existing = {k: v for k, v in final.items() if k.startswith(f'backup/{lab}/')}
            assert existing == ({} if lab in retired else ledger[lab]), 'recovery failed to finish retirement or damaged a survivor'
        for lab in orphans:
            assert not any(k.startswith(f'backup/{lab}/') for k in final), 'recovery leaked failed-backup storage'
        assert {k: v for k, v in final.items() if k.startswith(f'backup/{writer_label}/')} == primary.ledger()
        assert settings(s3, bucket) == original_settings, 'bucket configuration changed'
        response = s3.get_object(Bucket=bucket, Key=control[0])
        assert response['Body'].read() == control[1], 'unrelated object changed'
        response['Body'].close()
        assert not audits, '\n'.join(audits)
        return recovery_requests if cut == 'retirement' else request_count
    finally:
        if process and process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if primary:
            primary.kill()
        if relay:
            relay.close()
        try:
            for key in objects(s3, bucket, prefix=''):
                s3.delete_object(Bucket=bucket, Key=key)
            s3.delete_bucket(Bucket=bucket)
        except ClientError:
            pass
        s3.close()


@pytest.mark.parametrize('recovery', ['reconcile', 'expire'])
def test_maintenance_host_loss(recovery):
    baseline = recovery_case('recovery-baseline-' + recovery, recovery=recovery)
    failures = []
    for index in range(1, min(baseline, 200) + 1):
        try:
            recovery_case(f'loss-{recovery}-{index}', cut=index, recovery=recovery)
        except Exception as exc:
            failures.append(f'last request {index}: {exc}')
    assert not failures, '\n\n'.join(failures)


@pytest.mark.parametrize('recovery', ['reconcile', 'expire'])
def test_overlapping_maintenance(recovery):
    baseline = recovery_case('overlap-baseline-' + recovery, recovery=recovery)
    failures = []
    for index in range(1, min(baseline + 1, 200) + 1):
        try:
            recovery_case(f'overlap-{recovery}-{index}', rival_at=index, recovery=recovery)
        except Exception as exc:
            failures.append(f'competing recovery before request {index}: {exc}')
    assert not failures, '\n\n'.join(failures)


def test_interrupted_recovery():
    # Stop the original at the first observable retirement with storage still
    # present, irrespective of how it represents or orders that decision. Then
    # interrupt its replacement at each request in the replacement's own path.
    baseline = recovery_case('replay-baseline', cut='retirement')
    failures = []
    for index in range(1, min(baseline, 200) + 1):
        try:
            recovery_case(f'replay-{index}', cut='retirement', recovery_cut=index)
        except Exception as exc:
            failures.append(f'replacement last request {index}: {exc}')
    assert not failures, '\n\n'.join(failures)

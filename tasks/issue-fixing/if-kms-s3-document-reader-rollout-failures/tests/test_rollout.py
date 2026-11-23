import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from botocore.exceptions import ClientError
import pytest
from protocol import APP, Corpus, aws, variant, un64, plain, encode
from traffic import Traffic


@pytest.fixture(scope='session', autouse=True)
def environment():
    try:
        aws('s3').list_buckets()
        kms = aws('kms')
        arn = kms.create_key(Description='capability preflight')['KeyMetadata']['Arn']
        context = {'service': 'preflight', 'stage': 'check'}
        blob = kms.encrypt(KeyId=arn, Plaintext=b'probe', EncryptionContext=context)['CiphertextBlob']
        assert kms.decrypt(KeyId=arn, CiphertextBlob=blob, EncryptionContext=context)['Plaintext'] == b'probe'
        rejected = False
        try:
            kms.decrypt(KeyId=arn, CiphertextBlob=blob, EncryptionContext=variant(context, 0))
        except kms.exceptions.InvalidCiphertextException:
            rejected = True
        assert rejected, 'KMS context binding is not enforced'
        s3 = aws('s3')
        name = 'preflight-' + __import__('uuid').uuid4().hex[:18]
        s3.create_bucket(Bucket=name)
        etag = s3.put_object(Bucket=name, Key='object', Body=b'v1')['ETag']
        s3.put_object(Bucket=name, Key='object', Body=b'v2', IfMatch=etag)
        rejected = False
        try:
            s3.put_object(Bucket=name, Key='object', Body=b'stale', IfMatch=etag)
        except ClientError as exc:
            rejected = exc.response['ResponseMetadata']['HTTPStatusCode'] == 412
        finally:
            s3.close()
        assert rejected, 'S3 If-Match is not enforced'
        assert aws('s3').get_object(Bucket=name, Key='object')['Body'].read() == b'v2'
    except Exception as exc:
        pytest.fail(f'environment: emulator preflight failed: {exc}')


def env(endpoint):
    return {**os.environ, 'AWS_ENDPOINT_URL': endpoint, 'AWS_DEFAULT_REGION': 'us-east-1',
            'AWS_ACCESS_KEY_ID': 'test', 'AWS_SECRET_ACCESS_KEY': 'test',
            'AWS_REQUEST_CHECKSUM_CALCULATION': 'when_required', 'PYTHONPATH': APP,
            'PYTHONDONTWRITEBYTECODE': '1'}


def read_process(corpus, key, endpoint):
    with tempfile.TemporaryDirectory(prefix='reader-') as work:
        return subprocess.run([sys.executable, APP + '/newsops.py', 'decrypt', '--bucket', corpus.bucket,
                               '--key', key], cwd=work, env=env(endpoint), capture_output=True, text=True, timeout=10)


def test_reader_before_reconciliation():
    c = Corpus()
    traffic = Traffic(c)
    endpoint = traffic.proxy('reader')
    failures = []
    try:
        # A newer stamp can have old-client ciphertexts after an old rotate.
        c.old_write(c.keys[2], 0, rotate=True)
        doc = c.get(c.keys[2])
        doc['sops_version'] = '3.3.0'
        c.put(c.keys[2], doc)
        for key in random.sample(c.keys, len(c.keys)):
            try:
                result = read_process(c, key, endpoint)
                if result.returncode or result.stdout != c.expected[key] + '\n':
                    failures.append(f'{key}: rc={result.returncode}, stderr={result.stderr[-500:]}')
            except Exception as exc:
                failures.append(f'{key}: {exc}')
        assert not failures + traffic.errors, '\n'.join(failures + traffic.errors)
    finally:
        traffic.close()


def test_live_rollout(tmp_path):
    c = Corpus()
    traffic = Traffic(c)
    endpoints = [traffic.proxy(i) for i in range(2)]
    reader_endpoint = traffic.proxy('reader')
    pause_path = str(tmp_path / 'supervisor.sock')
    traffic.pauses(pause_path)
    logs, workdirs = [], []
    processes = {}
    starts = {}
    errors = []

    def remember(error):
        if len(errors) < 100:
            errors.append(str(error))

    def launch(index):
        work = tempfile.TemporaryDirectory(prefix='reconciler-')
        workdirs.append(work)
        log = open(tmp_path / f'worker-{index}-{len(logs)}.log', 'wb')
        logs.append(log)
        p = subprocess.Popen([sys.executable, APP + '/migrator.py', '--bucket', c.bucket,
                              '--prefix', 'docs/', '--barrier-key', 'control/legacy-fleet-retired'],
                             cwd=work.name, env={**env(endpoints[index]), 'SEAM_SOCKET': pause_path},
                             stdout=log, stderr=subprocess.STDOUT)
        processes[index] = p
        traffic.processes[index] = p
        starts[index] = starts.get(index, 0) + 1

    began = time.monotonic()
    for key in c.keys:
        c.due[key] = began + 15
    rotated = False
    retirement_at = began + 45 + random.uniform(0, 10)
    retired_at = None
    reader_cursor = 0
    final_passed = False
    try:
        launch(0)
        launch(1)
        while time.monotonic() < began + 80:
            tick = time.monotonic()
            for i, p in list(processes.items()):
                if p.poll() is not None:
                    if i in traffic.killed and starts[i] == 1:
                        # Let both first-generation workers reach the publication
                        # window before bringing a spare up; don't require both to
                        # write if a valid alternative design elects one worker.
                        if tick - traffic.killed[i] >= 2 and (len(traffic.killed) == 2 or tick - began > 6):
                            launch(i)
                    else:
                        remember(f'reconciler {i} exited unexpectedly ({p.returncode})')
            with c.lock:
                if not rotated and tick - began >= 20:
                    rotated = True
                    try:
                        c.old_write(c.keys[2], 0, rotate=True, write_pick=1)
                    except Exception as exc:
                        remember(f'legacy rotate: {exc}')
                if retired_at is None and tick >= retirement_at:
                    traffic.retired = True
                    c.s3.put_object(Bucket=c.bucket, Key='control/legacy-fleet-retired', Body=b'retired\n')
                    retired_at = time.monotonic()
                snapshots = {}
                for key in c.keys:
                    try:
                        doc = c.get(key)
                        snapshots[key] = doc
                        c.check(key, doc, legacy=retired_at is None,
                                fixed=time.monotonic() >= c.due[key], published=False)
                    except Exception as exc:
                        remember(f'probe: {exc}')
                if retired_at is not None and time.monotonic() >= retired_at + 20:
                    try:
                        for key in c.keys:
                            c.check(key, snapshots[key], legacy=False, fixed=True, final=True)
                        keys = {x['Key'] for x in c.s3.list_objects_v2(Bucket=c.bucket, Prefix='docs/').get('Contents', [])}
                        assert keys == set(c.keys), 'docs/ key set changed'
                        assert c.get('control/keys.json') == c.inventory, 'permission inventory changed'
                        final_passed = True
                    except Exception as exc:
                        remember(f'final convergence: {exc}')
                        break
            key = c.keys[reader_cursor % len(c.keys)]
            reader_cursor += 1
            try:
                result = read_process(c, key, reader_endpoint)
                if result.returncode or result.stdout.rstrip('\n') not in c.prior[key]:
                    remember(f'reader: {key}: rc={result.returncode}, stderr={result.stderr[-500:]}')
            except Exception as exc:
                remember(f'reader: {key}: {exc}')
            if final_passed:
                break
            time.sleep(max(0, 1 - (time.monotonic() - tick)))
        if not final_passed:
            remember('rollout did not reach final convergence')
        # A daemon that simply never processes documents is still scoreable.
        if not traffic.archive_edited:
            remember('reconciliation never reached publication for the editable archive')
        if not traffic.edited:
            remember('reconciliation never reached publication for the editable document')
        if not any(e.get('seam') == 'before_publish' and 'key' in e and 'etag' in e for e in traffic.announcements):
            remember('supervisor received no pre-publication announcements')
        for key in c.keys:
            try:
                result = read_process(c, key, reader_endpoint)
                assert result.returncode == 0 and result.stdout == c.expected[key] + '\n', f'{key}: final reader failed'
            except Exception as exc:
                remember(exc)
    finally:
        for p in processes.values():
            if p.poll() is None:
                p.terminate()
        limit = time.monotonic() + 5
        for i, p in processes.items():
            try:
                p.wait(timeout=max(0.05, limit - time.monotonic()))
            except subprocess.TimeoutExpired:
                remember(f'reconciler {i} did not stop on SIGTERM')
                p.kill()
                p.wait()
        traffic.close()
        for f in logs:
            f.close()
        for work in workdirs:
            work.cleanup()
        evidence = {'errors': errors, 'traffic_errors': traffic.errors,
                    'accepted_writes': traffic.audit, 'killed_roles': list(traffic.killed),
                    'edited': traffic.edited, 'archive_edited': traffic.archive_edited, 'permission_denials': traffic.denials, 'rotated': rotated, 'converged': final_passed}
        (tmp_path / 'observations.json').write_text(json.dumps(evidence, indent=2))
        out = Path('/logs/verifier')
        if out.exists():
            (out / 'observations.json').write_text(json.dumps(evidence, indent=2))
            for log in tmp_path.glob('worker-*.log'):
                (out / log.name).write_bytes(log.read_bytes())
    assert not errors + traffic.errors, '\n'.join(errors + traffic.errors)

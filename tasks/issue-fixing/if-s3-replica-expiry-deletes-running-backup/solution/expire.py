"""Restartable maintenance over a fixed repository observation."""
import logging
from botocore.exceptions import ClientError
from repository import LABEL, Repository, entry

log = logging.getLogger(__name__)


def observe(repo, count):
    info, etag = repo.load()
    objects = repo.objects()
    groups = {}
    for key in objects:
        parts = key.split('/')
        if len(parts) > 2 and LABEL.fullmatch(parts[1]):
            groups.setdefault(parts[1], set()).add('/'.join(parts[2:]))
    retired = set(info.get('retired', []))
    current = {}
    # Reconstruct only backups not covered by durable retirement authority.
    # Another maintainer may remove a manifest between our LIST and GET.
    for label, keys in groups.items():
        if label not in retired and 'backup.manifest' in keys:
            try:
                current[label] = entry(repo.manifest(label))
            except ClientError as exc:
                if exc.response['ResponseMetadata']['HTTPStatusCode'] != 404:
                    raise
                # Its deletion must have a preceding catalog publication. Do
                # not turn this observation into a new plan: load that decision.
                return None
    # A missing manifest for a previously current label is interrupted removal.
    retired.update(set(info['current']) - set(current))
    if count is not None:
        selected = set(sorted(current, reverse=True)[count:])
        retired.update(selected)
        for label in selected:
            del current[label]
    latest = max(groups, default=None)
    for label, keys in groups.items():
        if label in current or label in retired:
            continue
        if label == latest and 'backup.manifest.copy' in keys and 'backup.manifest' not in keys:
            continue
        retired.add(label)
    info['current'] = current
    # Keep retirement evidence even after the last object disappears. Besides
    # supporting cold recovery, this prevents an old observation from becoming
    # valid again if the catalog body would otherwise cycle back to its ETag.
    info['retired'] = sorted(retired)
    return info, etag, retired, groups


def maintain(bucket, count):
    repo = Repository(bucket)
    for _ in range(64):
        plan = observe(repo, count)
        if plan is None:
            continue
        info, etag, retired, groups = plan
        try:
            repo.save(info, etag)
        except ClientError as exc:
            if exc.response['ResponseMetadata']['HTTPStatusCode'] != 412:
                raise
            repo.reconnect()
            continue
        # The primary never reuses labels. Concurrent and replacement workers
        # may all replay this same immutable deletion authority safely.
        for label in sorted(retired & groups.keys(), reverse=True):
            log.info('remove retired backup %s', label)
            repo.delete(f'backup/{label}/backup.manifest')
            repo.delete(f'backup/{label}/backup.manifest.copy')
            repo.remove(label)
        log.info('maintenance complete')
        return
    raise RuntimeError('catalog changed too often to publish maintenance')


def expire(bucket, retention_full):
    maintain(bucket, retention_full)


def reconcile(bucket):
    maintain(bucket, None)

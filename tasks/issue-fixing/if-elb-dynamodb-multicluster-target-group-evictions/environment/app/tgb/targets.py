"""TargetsManager: an abstraction around ELBv2's targets API.

Targets of each target group are cached for TARGETS_CACHE_TTL seconds. A listing served from the
cache only re-describes the cached targets that are not healthy yet, so their state stays current
without re-listing the whole group. Successful RegisterTargets/DeregisterTargets calls are applied
to the cached entry so the next listing reflects them.
"""
import logging
import threading
import time

from .types import TargetInfo

log = logging.getLogger("tgb.targets")

TARGETS_CACHE_TTL = 300.0
REGISTER_TARGETS_CHUNK_SIZE = 200
DEREGISTER_TARGETS_CHUNK_SIZE = 200


def _chunks(items, size):
    for start in range(0, len(items), size):
        yield items[start:start + size]


class _CacheItem:
    def __init__(self, targets, expires_at):
        self.targets = targets
        self.expires_at = expires_at


class CachedTargetsManager:
    def __init__(self, elbv2, ttl=TARGETS_CACHE_TTL, clock=time.monotonic):
        self.elbv2 = elbv2
        self.ttl = ttl
        self.clock = clock
        self._cache = {}
        self._lock = threading.Lock()

    # -- mutations ---------------------------------------------------------------------------

    def register_targets(self, tg_arn, targets):
        for chunk in _chunks(list(targets), REGISTER_TARGETS_CHUNK_SIZE):
            log.info("registering targets %s", [_desc(t) for t in chunk])
            self.elbv2.register_targets(TargetGroupArn=tg_arn, Targets=chunk)
            log.info("registered targets")
            self._record_successful_register(tg_arn, chunk)

    def deregister_targets(self, tg_arn, targets):
        for chunk in _chunks(list(targets), DEREGISTER_TARGETS_CHUNK_SIZE):
            log.info("deregistering targets %s", [_desc(t) for t in chunk])
            self.elbv2.deregister_targets(TargetGroupArn=tg_arn, Targets=chunk)
            log.info("deregistered targets")
            self._record_successful_deregister(tg_arn, chunk)

    # -- listing -----------------------------------------------------------------------------

    def list_targets(self, tg_arn):
        with self._lock:
            item = self._cache.get(tg_arn)
            if item is not None and item.expires_at > self.clock():
                item.targets = self._refresh_unhealthy_targets(tg_arn, item.targets)
                return list(item.targets)
            targets = self._list_from_aws(tg_arn, None)
            self._cache[tg_arn] = _CacheItem(targets, self.clock() + self.ttl)
            return list(targets)

    def _refresh_unhealthy_targets(self, tg_arn, cached):
        refreshed = [t for t in cached if t.is_healthy()]
        unhealthy = [t.target for t in cached if not t.is_healthy()]
        if not unhealthy:
            return refreshed
        for target in self._list_from_aws(tg_arn, unhealthy):
            if target.is_not_registered():
                continue
            refreshed.append(target)
        return refreshed

    def _list_from_aws(self, tg_arn, targets):
        kwargs = {"TargetGroupArn": tg_arn}
        if targets:
            kwargs["Targets"] = [dict(t) for t in targets]
        resp = self.elbv2.describe_target_health(**kwargs)
        return [
            TargetInfo(target=dict(d["Target"]), health=dict(d.get("TargetHealth", {})))
            for d in resp.get("TargetHealthDescriptions", [])
        ]

    # -- cache bookkeeping -------------------------------------------------------------------

    def _record_successful_register(self, tg_arn, chunk):
        with self._lock:
            item = self._cache.get(tg_arn)
            if item is None:
                return
            known = {t.id for t in item.targets}
            for desc in chunk:
                info = TargetInfo(target=dict(desc), health={"State": "initial"})
                if info.id not in known:
                    item.targets.append(info)

    def _record_successful_deregister(self, tg_arn, chunk):
        with self._lock:
            item = self._cache.get(tg_arn)
            if item is None:
                return
            gone = {TargetInfo(target=dict(desc)).id for desc in chunk}
            item.targets = [t for t in item.targets if t.id not in gone]


def _desc(target):
    return f"{target['Id']}:{target['Port']}"

"""ResourceManager: reconciles one binding's endpoints into its target group (IP targets)."""
import logging

from .types import parse_identifier

log = logging.getLogger("tgb.reconcile")


def partition_targets_by_draining_status(targets):
    not_draining = [t for t in targets if not t.is_draining()]
    draining = [t for t in targets if t.is_draining()]
    return not_draining, draining


def match_endpoints_with_targets(endpoints, targets):
    """Match endpoints and targets on Id:Port.

    Returns (matched pairs, endpoints with no target, targets with no endpoint).
    """
    by_id = {t.id: t for t in targets}
    endpoint_ids = {e.id for e in endpoints}
    matched = [(e, by_id[e.id]) for e in endpoints if e.id in by_id]
    unmatched_endpoints = [e for e in endpoints if e.id not in by_id]
    unmatched_targets = [t for t in targets if t.id not in endpoint_ids]
    return matched, unmatched_endpoints, unmatched_targets


class ResourceManager:
    """The target group is shared with other clusters and hand-registered hosts. Only targets with an
    ownership item under this cluster, and none under another cluster, may be deregistered, and the
    item is the only thing that lets a restarted controller find a target again, so it is written
    before RegisterTargets and deleted only after DeregisterTargets has succeeded. A failed call may
    still have been applied, so nothing is rolled back on error.
    """

    def __init__(self, targets_manager, tg_arn, availability_zone, ledger):
        self.targets = targets_manager
        self.tg_arn = tg_arn
        self.availability_zone = availability_zone
        self.ledger = ledger

    def reconcile(self, binding):
        """One reconcile pass. Returns True once a deleted binding has been fully cleaned up."""
        # After an agent failover the file may hold an older snapshot than one already acted on,
        # possibly by a previous process. Acting on it would take live pods out and claim addresses
        # that may since have gone to the other cluster. The mark is recorded before acting.
        mark = self.ledger.generation_mark()
        if mark is not None and binding.generation < mark:
            log.warning("binding generation %d is older than %d, already acted on; waiting",
                        binding.generation, mark)
            return False
        if mark != binding.generation and not self.ledger.advance_generation(binding.generation):
            return False

        desired = {} if binding.deleted else {e.id: e for e in binding.endpoints}
        owned = self.ledger.owned()

        # Deregistering a target that is not registered succeeds, so every owned target that is
        # no longer wanted is deregistered whether or not the listing shows it. Addresses are
        # recycled across clusters: one that another cluster's items now name is that cluster's
        # pod, so the item is dropped and the target left alone.
        stale = sorted(owned - set(desired))
        if stale:
            handed_over = self.ledger.claimed_by_others(stale)
            mine = [t for t in stale if t not in handed_over]
            if mine:
                self.targets.deregister_targets(self.tg_arn, [self.description_for(t) for t in mine])
            self.ledger.release(stale)

        if binding.deleted:
            return True

        targets = self.targets.list_targets(self.tg_arn)
        not_draining, _ = partition_targets_by_draining_status(targets)
        _, unmatched_endpoints, _ = match_endpoints_with_targets(binding.endpoints, not_draining)

        unclaimed = {e.id for e in binding.endpoints} - owned
        if unmatched_endpoints or unclaimed:
            self.ledger.claim(unclaimed | {e.id for e in unmatched_endpoints})
        if unmatched_endpoints:
            self.register_endpoints(unmatched_endpoints)
        return False

    def deregister_targets(self, targets):
        self.targets.deregister_targets(self.tg_arn, [t.target for t in targets])

    def register_endpoints(self, endpoints):
        self.targets.register_targets(self.tg_arn, [self.target_description(e) for e in endpoints])

    def target_description(self, endpoint):
        return {"Id": endpoint.ip, "Port": endpoint.port, "AvailabilityZone": self.availability_zone}

    def description_for(self, identifier):
        ip, port = parse_identifier(identifier)
        return {"Id": ip, "Port": port, "AvailabilityZone": self.availability_zone}

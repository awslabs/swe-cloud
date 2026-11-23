"""ResourceManager: reconciles one binding's endpoints into its target group (IP targets)."""
import logging

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
    def __init__(self, targets_manager, tg_arn, availability_zone):
        self.targets = targets_manager
        self.tg_arn = tg_arn
        self.availability_zone = availability_zone

    def reconcile(self, binding):
        """One reconcile pass. Returns True once a deleted binding has been fully cleaned up."""
        if binding.deleted:
            self.cleanup()
            return True

        targets = self.targets.list_targets(self.tg_arn)
        not_draining, _ = partition_targets_by_draining_status(targets)
        _, unmatched_endpoints, unmatched_targets = match_endpoints_with_targets(
            binding.endpoints, not_draining)

        if unmatched_targets:
            self.deregister_targets(unmatched_targets)

        if unmatched_endpoints:
            self.register_endpoints(unmatched_endpoints)
        return False

    def cleanup(self):
        targets = self.targets.list_targets(self.tg_arn)
        if targets:
            self.deregister_targets(targets)

    def deregister_targets(self, targets):
        self.targets.deregister_targets(self.tg_arn, [t.target for t in targets])

    def register_endpoints(self, endpoints):
        self.targets.register_targets(self.tg_arn, [self.target_description(e) for e in endpoints])

    def target_description(self, endpoint):
        return {"Id": endpoint.ip, "Port": endpoint.port, "AvailabilityZone": self.availability_zone}


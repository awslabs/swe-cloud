"""Cordon-and-drain handler (pkg/interruptionevent/draincordon)."""

import logging

from .events import SPOT_ITN_KIND
from .kube import NotFound, Unreachable

log = logging.getLogger("nth.drain")

ALLOWED_KINDS = {SPOT_ITN_KIND}


class DrainCordonHandler:
    def __init__(self, store, cluster, config):
        self.store = store
        self.cluster = cluster
        self.config = config

    def handle_event(self, event):
        if event.kind not in ALLOWED_KINDS:
            return
        node_name = event.node_name
        if not node_name:
            raise ValueError(f"no node name for instance {event.instance_id}")

        node_found = True
        try:
            self.cluster.get_node(node_name)
        except NotFound:
            log.warning("node %s not found in the cluster", node_name)
            node_found = False
        except Unreachable:
            # Nothing can be done from here; let a replica that reaches the API have it.
            self.store.cancel_interruption_event(event.event_id)
            self._run_cancel_drain_task(event)
            raise
        except Exception as err:  # noqa: BLE001
            log.warning("failed to get node %s, proceeding: %s", node_name, err)

        if event.pre_drain_task is not None:
            try:
                event.pre_drain_task(event)
            except Exception as err:  # noqa: BLE001
                log.error("pre-drain task failed for %s; aborting to allow SQS retry: %s",
                          node_name, err)
                self.store.cancel_interruption_event(event.event_id)
                self._run_cancel_drain_task(event)
                if (not node_found and self.config.delete_sqs_msg_if_node_not_found
                        and event.post_drain_task is not None):
                    self._run_post_drain_task(event)
                    return
                raise

        err = None
        try:
            self.cluster.cordon_and_drain(node_name, self.config.node_termination_grace_period)
        except Exception as exc:  # noqa: BLE001
            err = exc
            log.error("there was a problem while trying to cordon and drain node %s: %s",
                      node_name, exc)

        if err is not None:
            self.store.cancel_interruption_event(event.event_id)
            if not (not node_found and self.config.delete_sqs_msg_if_node_not_found):
                self._run_cancel_drain_task(event)
        else:
            log.info("node %s successfully cordoned and drained", node_name)
            self.store.mark_all_as_processed(node_name)

        if ((err is None or (not node_found and self.config.delete_sqs_msg_if_node_not_found))
                and event.post_drain_task is not None):
            self._run_post_drain_task(event)

    def _run_cancel_drain_task(self, event):
        if event.cancel_drain_task is not None:
            try:
                event.cancel_drain_task(event)
            except Exception as err:  # noqa: BLE001
                log.error("cancel-drain task failed for %s: %s", event.node_name, err)

    def _run_post_drain_task(self, event):
        try:
            event.post_drain_task(event)
            log.info("post-drain task completed for %s", event.node_name)
        except Exception as err:  # noqa: BLE001
            log.error("post-drain task failed for %s: %s", event.node_name, err)

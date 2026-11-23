"""Entry point: `python3 -m nth` (cmd/node-termination-handler.go, queue-processor mode)."""

import logging
import queue
import signal
import sys
import threading
import time

import boto3

from . import VERSION
from .config import ConfigError, load
from .drain import DrainCordonHandler
from .event_store import Store
from .kube import ClusterClient
from .sqs_monitor import SQSMonitor

log = logging.getLogger("nth")

MONITOR_INTERVAL_SEC = 2.0
INTERRUPTION_LOOP_INTERVAL_SEC = 1.0
STARTUP_ATTEMPTS = 30


def setup_logging(config):
    level = getattr(logging, config.log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level, stream=sys.stdout,
        format=f"%(asctime)s %(levelname)s [{config.pod_name}] %(name)s: %(message)s")
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def wait_for_cluster(cluster):
    for attempt in range(1, STARTUP_ATTEMPTS + 1):
        try:
            nodes = cluster.list_nodes()
            log.info("cluster API reachable, %d nodes", len(nodes))
            return
        except Exception as err:  # noqa: BLE001
            log.warning("cluster API not reachable yet (attempt %d): %s", attempt, err)
            time.sleep(1)
    raise SystemExit("cluster API unreachable, giving up")


def run_monitor(monitor, stop):
    log.info("started monitoring for %s events", monitor.kind())
    while not stop.is_set():
        started = time.monotonic()
        try:
            monitor.monitor()
        except Exception as err:  # noqa: BLE001
            log.warning("there was a problem monitoring for %s events: %s", monitor.kind(), err)
        # time.Tick semantics: one call per interval, a slow call is followed at once.
        remaining = MONITOR_INTERVAL_SEC - (time.monotonic() - started)
        if remaining > 0:
            stop.wait(remaining)


def watch_for_interruption_events(interruptions, store):
    while True:
        store.add_interruption_event(interruptions.get())


def process_interruption_event(store, event, handler):
    try:
        handler.handle_event(event)
    except Exception as err:  # noqa: BLE001
        log.error("handling event %s: %s", event.event_id, err)
    finally:
        store.workers.release()


def main():
    try:
        config = load()
    except ConfigError as err:
        print(f"invalid configuration: {err}", file=sys.stderr)
        return 1
    setup_logging(config)
    log.info("aws-node-termination-handler %s starting in queue-processor mode", VERSION)
    log.info("queue %s, visibility timeout %ss, workers %d",
             config.queue_url, config.sqs_msg_visibility_timeout_sec, config.workers)

    sqs = boto3.client("sqs")
    cluster = ClusterClient(config.cluster_api_url, config.cluster_api_token, config.pod_name)
    wait_for_cluster(cluster)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    interruptions = queue.Queue()
    store = Store(config)
    monitor = SQSMonitor(config, sqs, cluster, interruptions)
    handler = DrainCordonHandler(store, cluster, config)

    threading.Thread(target=run_monitor, args=(monitor, stop), daemon=True,
                     name="sqs-monitor").start()
    threading.Thread(target=watch_for_interruption_events, args=(interruptions, store),
                     daemon=True, name="event-watcher").start()
    log.info("aws-node-termination-handler has started successfully")

    workers = []
    while not stop.wait(INTERRUPTION_LOOP_INTERVAL_SEC):
        while True:
            event = store.get_active_event()
            if event is None:
                break
            if not store.workers.acquire(blocking=False):
                log.warning("all workers busy, waiting")
                break
            log.info("processing interruption event %s for node %s", event.event_id,
                     event.node_name)
            event.in_progress = True
            worker = threading.Thread(target=process_interruption_event,
                                      args=(store, event, handler), name=f"drain-{event.node_name}")
            worker.start()
            workers.append(worker)
        workers = [w for w in workers if w.is_alive()]

    log.info("aws-node-termination-handler is shutting down")
    for worker in workers:
        worker.join()
    log.info("all event processors finished")
    return 0


if __name__ == "__main__":
    sys.exit(main())

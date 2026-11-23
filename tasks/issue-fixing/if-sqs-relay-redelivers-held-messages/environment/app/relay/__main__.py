"""Entry point: ``python -m relay`` (configuration from the environment, see README.md)."""

import argparse
import logging
import os
import signal
import sys

from relay.config import RelayConfig
from relay.consumer import Consumer


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m relay",
        description="Relay SQS messages to the downstream, one at a time. "
                    "Configured through the environment: QUEUE_URL, DOWNSTREAM_URL, RELAY_ID, "
                    "AWS_ENDPOINT_URL_SQS, VISIBILITY_TIMEOUT, MAX_MESSAGES_PER_POLL, WAIT_TIME_SECONDS, "
                    "CONCURRENT_REQUEST_LIMIT, EXTEND_MESSAGE_VISIBILITY, POLL_DELAY_MS, LOG_LEVEL.")
    parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    for noisy in ("botocore", "boto3", "urllib3"):
        logging.getLogger(noisy).setLevel(max(logging.getLogger().level, logging.INFO))
    config = RelayConfig.from_env()
    consumer = Consumer(config)

    def on_signal(signum, _frame):
        logging.getLogger("relay").info("signal %s received, stopping", signum)
        consumer.request_stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    logging.getLogger("relay").info("relay %s consuming %s", config.relay_id, config.queue_url)
    consumer.run()
    logging.getLogger("relay").info("relay %s stopped", config.relay_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())

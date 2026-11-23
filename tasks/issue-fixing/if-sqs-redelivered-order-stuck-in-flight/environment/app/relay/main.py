import logging
import signal
import sys

from .fulfilment import FulfilmentClient
from .receiver import MessageReceiver
from .settings import ReceiveSettings
from .sqs import ClientContext


def main():
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s",
    )
    logging.getLogger("botocore").setLevel(logging.WARNING)
    settings = ReceiveSettings.from_env()
    client = ClientContext(settings)
    receiver = MessageReceiver(settings, client, FulfilmentClient(settings))

    def stop(signum, frame):
        receiver.stop()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    receiver.run()
    return 0

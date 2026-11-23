import threading

import boto3
import botocore.config

_local = threading.local()

_client_config = botocore.config.Config(retries={'mode': 'standard', 'max_attempts': 3},
                                        connect_timeout=5,
                                        read_timeout=30)


def client(service: str):
    """
    A boto3 client for the given service, one per thread. The endpoint,
    region and credentials come from the environment.
    """
    clients = getattr(_local, 'clients', None)
    if clients is None:
        clients = _local.clients = {}
    try:
        return clients[service]
    except KeyError:
        clients[service] = boto3.session.Session().client(service, config=_client_config)
        return clients[service]

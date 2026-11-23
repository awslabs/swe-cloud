"""Retry policy shared by the gateway-facing application paths."""
import time
from botocore.exceptions import (ClientError, ConnectionClosedError, ConnectTimeoutError,
                                 EndpointConnectionError, ReadTimeoutError)


def transient(exc):
    if isinstance(exc, (ConnectionClosedError, ConnectTimeoutError, EndpointConnectionError, ReadTimeoutError)):
        return True
    return (isinstance(exc, ClientError) and exc.response.get('Error', {}).get('Code') in {
        'InternalServerError', 'ServiceUnavailable', 'RequestLimitExceeded',
        'ProvisionedThroughputExceededException', 'ThrottlingException', 'TransactionInProgressException'})


def call(operation, ctx):
    delay = 0.03
    while True:
        try:
            return operation()
        except Exception as exc:
            if not transient(exc) or ctx.get_remaining_time_in_millis() <= 150:
                raise
            time.sleep(min(delay, ctx.get_remaining_time_in_millis() / 2000))
            delay = min(delay * 2, 0.15)

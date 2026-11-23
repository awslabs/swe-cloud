import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class SqsError(Exception):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code = code


class MessageNotInflight(SqsError):
    pass


class ReceiptHandleIsInvalid(SqsError):
    pass


_NOT_INFLIGHT = {"AWS.SimpleQueueService.MessageNotInflight", "MessageNotInflight"}
_INVALID_HANDLE = {"ReceiptHandleIsInvalid", "InvalidParameterValue", "AWS.SimpleQueueService.ReceiptHandleIsInvalid"}


def _translate(error):
    code = error.response.get("Error", {}).get("Code", "")
    message = error.response.get("Error", {}).get("Message", "")
    if code in _NOT_INFLIGHT:
        return MessageNotInflight(code, message)
    if code in _INVALID_HANDLE:
        return ReceiptHandleIsInvalid(code, message)
    return SqsError(code, message)


class ClientContext:
    """The one SQS client the relay uses; every call goes to settings.endpoint_url."""

    def __init__(self, settings):
        self._settings = settings
        self._client = boto3.client(
            "sqs",
            endpoint_url=settings.endpoint_url,
            region_name=settings.region,
            config=Config(
                max_pool_connections=64,
                connect_timeout=5,
                read_timeout=30,
                retries={"mode": "standard", "max_attempts": 3},
            ),
        )

    def queue_attributes(self):
        try:
            response = self._client.get_queue_attributes(QueueUrl=self._settings.queue_url, AttributeNames=["All"])
        except ClientError as error:
            raise _translate(error) from None
        return response.get("Attributes", {})

    def receive(self, max_messages, wait_time_seconds):
        try:
            response = self._client.receive_message(
                QueueUrl=self._settings.queue_url,
                MaxNumberOfMessages=max_messages,
                WaitTimeSeconds=wait_time_seconds,
                AttributeNames=["All"],
                MessageAttributeNames=["All"],
            )
        except ClientError as error:
            raise _translate(error) from None
        return response.get("Messages", [])

    def delete(self, receipt_handle):
        try:
            self._client.delete_message(QueueUrl=self._settings.queue_url, ReceiptHandle=receipt_handle)
        except ClientError as error:
            raise _translate(error) from None

    def change_visibility(self, receipt_handle, timeout):
        try:
            self._client.change_message_visibility(
                QueueUrl=self._settings.queue_url, ReceiptHandle=receipt_handle, VisibilityTimeout=int(timeout)
            )
        except ClientError as error:
            raise _translate(error) from None

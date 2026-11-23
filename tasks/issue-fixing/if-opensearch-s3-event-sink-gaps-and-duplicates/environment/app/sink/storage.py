"""S3 client lifetime: independent pools per calling thread."""
import threading
import boto3


class Storage:
    def __init__(self):
        self.local = threading.local()

    @property
    def client(self):
        if not hasattr(self.local, 'client'):
            self.local.client = boto3.client('s3')
        return self.local.client

    def reset_client(self):
        # Floci leaves unread PUT bytes after a rejected conditional request.
        # Never return that connection to a subsequent request's pool.
        if hasattr(self.local, 'client'):
            self.local.client.close()
            del self.local.client

import os

OS_ENDPOINT = os.environ.get('OS_ENDPOINT', 'http://127.0.0.1:9201').rstrip('/')
WRITE_ALIAS = os.environ.get('WRITE_ALIAS', 'events')
QUERY_TERM = os.environ.get('QUERY_TERM', 'key')
DLQ_BUCKET = os.environ.get('DLQ_BUCKET', 'dlq-bucket')
DLQ_PREFIX = os.environ.get('DLQ_PREFIX', 'dlq/')
MAX_LATE_APPLY_S = float(os.environ.get('MAX_LATE_APPLY_S', '8'))
BULK_MAX_DOCS = 10
FLUSH_INTERVAL_S = 0.25
QUERY_INTERVAL_S = 2
QUERY_DURATION_S = 3

INBOX_BUCKET = os.environ.get('INBOX_BUCKET', DLQ_BUCKET)
INBOX_PREFIX = os.environ.get('INBOX_PREFIX', 'inbox/')

WORKER_ID = os.environ.get('WORKER_ID', 'standalone')
INTAKE_PORT = int(os.environ.get('INTAKE_PORT', '8080'))

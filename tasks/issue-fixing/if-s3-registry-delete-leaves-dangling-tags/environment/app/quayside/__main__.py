"""Run one registry replica.

    python3 -m quayside --listen 127.0.0.1:5001 --s3-endpoint http://localhost:4566 \
        --bucket registry --replica-id a
"""

import argparse
import logging
import sys

from .blobstore import BlobStore
from .driver import S3Driver
from .handlers import Registry
from .server import serve


def main(argv=None):
    parser = argparse.ArgumentParser(prog="quayside")
    parser.add_argument("--listen", required=True, help="host:port to serve HTTP on")
    parser.add_argument("--s3-endpoint", required=True, help="S3 endpoint URL")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--replica-id", required=True)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--concurrency", type=int, default=10,
                        help="parallel storage operations per request")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format=f"%(asctime)s replica={args.replica_id} %(name)s %(levelname)s %(message)s",
    )
    host, _, port = args.listen.rpartition(":")
    driver = S3Driver(args.s3_endpoint, args.bucket, args.region)
    registry = Registry(BlobStore(driver), args.replica_id, args.concurrency)
    serve(registry, host or "127.0.0.1", int(port))


if __name__ == "__main__":
    main()

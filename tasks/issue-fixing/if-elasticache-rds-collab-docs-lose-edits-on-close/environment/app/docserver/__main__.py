"""docserver --instance-id I1 --http-port 7101

Environment: PG_DSN (the docserver role on the documents database), VALKEY_URL, KEY_PREFIX.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

from .database import DatabaseExtension
from .http_api import serve
from .redis_ext import RedisExtension
from .server import DocServer


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docserver", description="Collaborative document server instance.")
    parser.add_argument("--instance-id", required=True)
    parser.add_argument("--http-port", type=int, required=True)
    args = parser.parse_args(argv)

    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format=f"%(asctime)s {args.instance_id} %(name)s %(levelname)s %(message)s",
    )
    extensions = [
        RedisExtension(os.environ["VALKEY_URL"], os.environ.get("KEY_PREFIX", "p"), args.instance_id),
        DatabaseExtension(os.environ["PG_DSN"], args.instance_id),
    ]
    serve(DocServer(args.instance_id, extensions), args.http_port)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Entry point: python3 -m cachesvc (configuration from the environment, see README.md)."""

import logging
import signal
import sys

from . import config as config_mod
from .disk import DiskCache
from .s3proxy import S3Cache
from .server import make_server


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = config_mod.from_env()

    proxy = S3Cache(
        endpoint=cfg.s3_endpoint,
        bucket=cfg.bucket,
        prefix=cfg.prefix,
        update_timestamps=cfg.update_timestamps,
        cas_refresh_age=cfg.cas_refresh_age,
        num_uploaders=cfg.num_uploaders,
        max_queued_uploads=cfg.max_queued_uploads,
    )
    cache = DiskCache(cfg.disk_dir, cfg.disk_max_bytes, proxy=proxy,
                      proxy_contains_workers=cfg.proxy_contains_workers)

    server = make_server(cfg.host, cfg.port, cache)

    def stop(*_):
        logging.getLogger("cachesvc").info("shutting down")
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    logging.getLogger("cachesvc").info(
        "listening on %s, bucket %s via %s, disk %s (%d bytes), cas_refresh_age %.1fs",
        cfg.listen, cfg.bucket, cfg.s3_endpoint, cfg.disk_dir, cfg.disk_max_bytes,
        cfg.cas_refresh_age)
    server.serve_forever()


if __name__ == "__main__":
    main()

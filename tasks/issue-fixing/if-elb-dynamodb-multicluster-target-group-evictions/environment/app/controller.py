"""Cluster target group binding controller.

One process per cluster. Every period it reads the cluster's binding file and reconciles the
binding's endpoints into the target group. See README.md for the deployment it runs in.
"""
import argparse
import logging
import os
import sys
import time

from tgb.aws import make_clients
from tgb.binding import load_binding
from tgb.reconcile import ResourceManager
from tgb.targets import CachedTargetsManager

log = logging.getLogger("tgb.controller")


def parse_args(argv):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cluster-id", required=True, help="cluster this controller acts for, e.g. cluster-a")
    p.add_argument("--target-group-arn", required=True)
    p.add_argument("--binding-file", required=True, help="JSON written by the cluster's endpoint agent")
    p.add_argument("--aws-endpoint", required=True, help="the cluster's AWS egress endpoint")
    p.add_argument("--ledger-table", required=True, help="DynamoDB ownership table (README: Ownership table)")
    p.add_argument("--availability-zone", required=True, help="AvailabilityZone sent with every target")
    p.add_argument("--scratch-dir", required=True, help="per-process working directory")
    p.add_argument("--period", type=float, default=1.0, help="seconds between reconciles")
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr,
        format=f"%(asctime)s {args.cluster_id} %(name)s %(levelname)s %(message)s")
    os.makedirs(args.scratch_dir, exist_ok=True)

    elbv2, _dynamodb = make_clients(args.aws_endpoint)
    manager = ResourceManager(CachedTargetsManager(elbv2), args.target_group_arn, args.availability_zone)

    log.info("starting: tg=%s binding=%s", args.target_group_arn, args.binding_file)
    while True:
        started = time.monotonic()
        try:
            binding = load_binding(args.binding_file)
        except (OSError, ValueError) as exc:
            log.warning("binding unreadable, skipping this period: %s", exc)
            binding = None
        if binding is not None:
            try:
                if manager.reconcile(binding):
                    log.info("binding deleted and cleaned up; exiting")
                    return 0
            except Exception:  # requeue: try again next period
                log.exception("reconcile failed")
        time.sleep(max(0.0, args.period - (time.monotonic() - started)))


if __name__ == "__main__":
    sys.exit(main())

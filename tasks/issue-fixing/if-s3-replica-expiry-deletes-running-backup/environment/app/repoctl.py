#!/usr/bin/env python3
"""Full-backup repository administration."""
import argparse
import json
import logging
import sys
from pathlib import Path
import boto3

sys.path.insert(0, str(Path(__file__).resolve().parent))
from infra.cfn_deploy import deploy
from backup import backup
from expire import expire, reconcile
from repository import LABEL, Repository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "backup", "expire", "reconcile", "info", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--bucket", required=True)
        if name == "backup":
            command.add_argument("--label", required=True)
            command.add_argument("--files", type=int, default=8)
        if name == "expire":
            command.add_argument("--retention-full", type=int, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.command == "expire":
        if args.retention_full < 1:
            parser.error("retention-full must be positive")
        expire(args.bucket, args.retention_full)
    elif args.command == "reconcile":
        reconcile(args.bucket)
    elif args.command == "backup":
        if not LABEL.fullmatch(args.label) or args.files < 1:
            parser.error("invalid backup label or file count")
        backup(args.bucket, args.label, args.files)
    else:
        repo = Repository(args.bucket)
        if args.command == "init":
            credentials_log = logging.getLogger('botocore.credentials')
            previous_level = credentials_log.level
            credentials_log.setLevel(logging.WARNING)
            try:
                deploy(boto3.client('cloudformation', endpoint_url=repo.s3.meta.endpoint_url,
                                    region_name=repo.s3.meta.region_name,
                                    aws_access_key_id='test', aws_secret_access_key='test'),
                       'seed-' + args.bucket, Path(__file__).parent / 'infra/seed.template.json',
                       BucketName=args.bucket)
            finally:
                credentials_log.setLevel(previous_level)
            repo.save({"format": 1, "current": {}})
            return
        info, _ = repo.load()
        if args.command == "info":
            print(json.dumps(info, indent=2, sort_keys=True))
        else:
            missing = []
            for label, item in info["current"].items():
                prefix = f"backup/{label}/"
                for suffix in ["backup.manifest"] + [f"pg_data/{n:04d}" for n in range(item["files"])]:
                    if not repo.exists(prefix + suffix):
                        missing.append(prefix + suffix)
            print(json.dumps({"status": "invalid" if missing else "ok", "missing": missing}))
            if missing:
                raise SystemExit(1)


if __name__ == "__main__":
    main()

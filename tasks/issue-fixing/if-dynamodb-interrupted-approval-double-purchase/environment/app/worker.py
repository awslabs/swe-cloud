import argparse
import json
import sys
import uuid
from decimal import Decimal

import manager
import retry as retries
from storage import Store, now_ms


def main():
    parser = argparse.ArgumentParser(description='Commitment purchase operations')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='Create the execution ledger')
    submit = commands.add_parser('submit', help='Record recommendations from a JSON file')
    submit.add_argument('--file', required=True)
    approve = commands.add_parser('approve', help='Approve and synchronously execute an order')
    approve.add_argument('--execution-id', required=True)
    approve.add_argument('--actor', required=True)
    retry = commands.add_parser('retry', help='Create or retrieve an operator retry order')
    retry.add_argument('--execution-id', required=True)
    sweep = commands.add_parser('sweep', help='Run scheduled purchase maintenance')
    sweep.add_argument('--once', action='store_true', required=True)
    args = parser.parse_args()
    if args.command == 'init':
        Store().init()
    elif args.command == 'submit':
        with open(args.file) as handle:
            recs = json.load(handle, parse_float=Decimal)
        for i, rec in enumerate(recs):
            rec.setdefault('index', i)
            rec.setdefault('count', 1)
            rec.setdefault('selected', True)
        row = dict(execution_id=str(uuid.uuid4()), status='pending', updated_at=now_ms(), recommendations=recs)
        Store().save(row)
        print(row['execution_id'])
    elif args.command == 'retry':
        print(retries.retry(args.execution_id))
    elif args.command == 'approve':
        manager.approve(args.execution_id, args.actor)
    else:
        print(json.dumps(manager.sweep()))


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

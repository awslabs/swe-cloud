import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from aws import client
from workers import Workers


def initialize():
    db = client()
    specs = [('DISPATCH_TABLE', 'dispatch_reservations', [('id', 'S')]),
             ('EXECUTION_TABLE', 'durable_executions', [('execution_id', 'S')]),
             ('IDEMPOTENCY_TABLE', 'idempotency', [('id', 'S')]),
             ('CHECKPOINT_TABLE', 'durable_checkpoints', [('execution_id', 'S'), ('seq', 'N')]),
             ('EFFECTS_TABLE', 'effects', [('payload_key', 'S'), ('execution_id', 'S')])]
    existing = db.list_tables()['TableNames']
    for variable, default, keys in specs:
        name = os.environ.get(variable, default)
        if name in existing:
            continue
        db.create_table(TableName=name, BillingMode='PAY_PER_REQUEST',
                        KeySchema=[{'AttributeName': k, 'KeyType': 'HASH' if i == 0 else 'RANGE'}
                                   for i, (k, _) in enumerate(keys)],
                        AttributeDefinitions=[{'AttributeName': k, 'AttributeType': t} for k, t in keys])
        db.get_waiter('table_exists').wait(TableName=name)


def main():
    parser = argparse.ArgumentParser(description='Order dispatch service')
    commands = parser.add_subparsers(dest='command', required=True)
    submit = commands.add_parser('submit', help='Accept and run one order to a receipt')
    submit.add_argument('--event', required=True, help='Order JSON')
    submit.add_argument('--execution-timeout', type=float, default=45)
    submit.add_argument('--invocation-timeout', type=float, default=4)
    args = parser.parse_args()
    initialize()
    request = {'event': json.loads(args.event), 'execution_id': str(uuid.uuid4()),
               'execution_start_ms': int(time.time() * 1000),
               'execution_timeout_ms': int(args.execution_timeout * 1000), 'invocation_timeout_s': args.invocation_timeout}
    deadline = request['execution_start_ms'] + request['execution_timeout_ms']
    with Workers() as workers:
        while time.time() * 1000 < deadline:
            with tempfile.TemporaryDirectory(prefix='dispatch-') as directory:
                process = workers.start(request, directory)
                try:
                    stdout, stderr = process.communicate(timeout=request['invocation_timeout_s'])
                except subprocess.TimeoutExpired:
                    workers.retire(process)
                    print(json.dumps({'status': 'RETRYING', 'error': 'InvocationTimeout'}), flush=True)
                    continue
                if process.returncode:
                    sys.stderr.write(stderr)
                    raise SystemExit(process.returncode)
                output = json.loads(stdout.strip().splitlines()[-1])
                print(json.dumps(output), flush=True)
            if output['status'] != 'SUSPENDED':
                return
            time.sleep(max(0, (min(output['resume_at_ms'], deadline) - time.time() * 1000) / 1000))
    print(json.dumps({'status': 'TIMED_OUT'}))


if __name__ == '__main__':
    main()

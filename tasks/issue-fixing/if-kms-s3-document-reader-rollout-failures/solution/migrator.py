import argparse
import signal
import sys
import time
from botocore.exceptions import ClientError
from migration import candidate
from permissions import KMSPrincipal
from seam import seam
from transport import client, load, encode

running = True


def stop(*_):
    global running
    raise SystemExit(0)


def main():
    parser = argparse.ArgumentParser(description='Reconcile the document fleet.')
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--prefix', required=True)
    parser.add_argument('--barrier-key', required=True)
    args = parser.parse_args()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    s3, kms = client('s3'), client('kms')
    inventory, _ = load(s3, args.bucket, 'control/keys.json')
    kms = KMSPrincipal(kms, inventory)
    checked = {}
    while running:
        try:
            try:
                s3.head_object(Bucket=args.bucket, Key=args.barrier_key)
                retired = True
            except ClientError as exc:
                if exc.response['ResponseMetadata']['HTTPStatusCode'] != 404:
                    raise
                retired = False
            pages = s3.get_paginator('list_objects_v2').paginate(Bucket=args.bucket, Prefix=args.prefix)
            for page in pages:
                for item in page.get('Contents', []):
                    if not running:
                        return
                    key = item['Key']
                    try:
                        doc, etag = load(s3, args.bucket, key)
                        if checked.get(key) == (etag, retired):
                            continue
                        result = candidate(kms, doc, key, retired, kms.decrypt_only)
                        if result is not None:
                            seam('before_publish', key=key, etag=etag)
                            response = s3.put_object(Bucket=args.bucket, Key=key, Body=encode(result), IfMatch=etag)
                            etag = response['ETag']
                        checked[key] = (etag, retired)
                    except ClientError as exc:
                        if exc.response['ResponseMetadata']['HTTPStatusCode'] in (409, 412):
                            s3.close()
                            s3 = client('s3')
                            continue
                        raise
                    except Exception as exc:
                        print(f'{key}: {exc}', file=sys.stderr, flush=True)
        except Exception as exc:
            print(str(exc), file=sys.stderr, flush=True)
            s3.close()
            s3 = client('s3')
        time.sleep(0.25)


if __name__ == '__main__':
    main()

"""Create a small ordinary deployment. Never replaces an existing bucket."""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from infra.cfn_deploy import deploy
from codec import b64, now, seal
from legacy import request_context
from shares import split
from transport import client, encode


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bucket', default='sopsdocs')
    args = parser.parse_args()
    s3, kms = client('s3'), client('kms')
    if any(b['Name'] == args.bucket for b in s3.list_buckets()['Buckets']):
        raise SystemExit('bucket exists; refusing to replace operator data')
    outputs = deploy(client('cloudformation'), 'document-seed-' + args.bucket,
                     Path(__file__).parent / 'infra/seed.template.json', BucketName=args.bucket)
    arn = outputs['Key0Arn']
    s3.put_object(Bucket=args.bucket, Key='control/keys.json', Body=encode({'arns': [arn]}))
    for name, context in [('accounts', {'app': 'accounts', 'env': 'prod'}),
                          ('roster', {'app': 'roster'})]:
        key = f'docs/{name}.json'
        data_key = os.urandom(32)
        entry = {'arn': arn, 'context': context, 'created_at': now(),
                 'enc': b64(kms.encrypt(KeyId=arn, Plaintext=data_key,
                            EncryptionContext=request_context(context, 0))['CiphertextBlob'])}
        doc = {'sops_version': '3.2.0', 'lastmodified': now(), 'kms': [entry],
               'payload': seal(data_key, f'{name} deployment settings', key)}
        s3.put_object(Bucket=args.bucket, Key=key, Body=encode(doc))
    key = 'docs/treasury.json'
    data_key = os.urandom(32)
    parts = split(data_key, [3, 11, 27], 2)
    entries = []
    for g in parts:
        wrapping_arn = kms.create_key(Description=f'treasury group {g}')['KeyMetadata']['Arn']
        context = {'app': 'treasury'} if g == 3 else {'app': 'treasury', 'env': 'prod'}
        entries.append({'group': g, 'arn': wrapping_arn, 'context': context, 'created_at': now(),
                        'enc': b64(kms.encrypt(KeyId=wrapping_arn, Plaintext=parts[g],
                                              EncryptionContext=request_context(context, 0))['CiphertextBlob'])})
    s3.put_object(Bucket=args.bucket, Key=key, Body=encode({
        'sops_version': '3.2.0', 'lastmodified': now(), 'shamir_threshold': 2,
        'kms': entries, 'payload': seal(data_key, 'treasury deployment settings', key)}))
    inventory = {arn} | {e['arn'] for e in entries}
    # An ordinary, already-direct recovery archive. Policy is separate from its
    # document: an old edit can drop unknown fields without changing permissions.
    key = 'docs/archive.json'
    data_key = os.urandom(32)
    parts = split(data_key, [7, 19, 43], 2)
    restricted = kms.create_key(Description='imported archive key')['KeyMetadata']['Arn']
    inventory.add(restricted)
    entries = []
    for g in parts:
        wrapping_arn = restricted if g == 7 else arn
        context = {'app': 'archive', 'env': 'prod'}
        entries.append({'group': g, 'arn': wrapping_arn, 'context': context, 'created_at': now(),
                        'enc': b64(kms.encrypt(KeyId=wrapping_arn, Plaintext=parts[g],
                                              EncryptionContext=context)['CiphertextBlob'])})
    s3.put_object(Bucket=args.bucket, Key=key, Body=encode({
        'sops_version': '3.2.0', 'lastmodified': now(), 'shamir_threshold': 2,
        'kms': entries, 'payload': seal(data_key, 'archive deployment settings', key)}))
    s3.put_object(Bucket=args.bucket, Key='control/keys.json',
                  Body=encode({'arns': sorted(inventory), 'decrypt_only': [restricted]}))
    print(f'Created {args.bucket}; no retirement signal has been published.')


if __name__ == '__main__':
    main()

"""The deployed 3.2 client. It does not call the new keyservice module."""
import argparse
import copy
import os
from shares import group, threshold, split, combine, material
from codec import b64, un64, now, seal, open_payload
from transport import client, load, encode


def request_context(context, pick):
    if not context:
        return {}
    value = context[sorted(context)[pick % len(context)]]
    return {name: value for name in context}


def read(kms, doc, key, pick):
    # Finish each group before visiting the next; retain every successful part.
    found = {}
    for g in dict.fromkeys(group(e) for e in doc['kms']):
        for entry in (e for e in doc['kms'] if group(e) == g):
            try:
                part = kms.decrypt(KeyId=entry['arn'], CiphertextBlob=un64(entry['enc']),
                                   EncryptionContext=request_context(entry['context'], pick))['Plaintext']
                if threshold(doc) > 1 and (len(part) != 33 or part[0] != g):
                    continue
                if threshold(doc) == 1:
                    try:
                        return part, open_payload(doc, part, key)
                    except Exception:
                        continue
                found[g] = part
                break
            except Exception:
                continue
    if threshold(doc) == 1:
        for part in found.values():
            try:
                return part, open_payload(doc, part, key)
            except Exception:
                continue
    elif len(found) >= threshold(doc):
        parts = list(found.values())
        data_key = combine(parts)
        return data_key, open_payload(doc, data_key, key)
    raise ValueError('no usable master key quorum')


def known_fields(doc):
    result = {name: copy.deepcopy(doc[name]) for name in ('sops_version', 'lastmodified', 'payload', 'kms')}
    if 'shamir_threshold' in doc:
        result['shamir_threshold'] = doc['shamir_threshold']
    result['kms'] = [{name: e[name] for name in ('arn', 'context', 'enc', 'created_at', 'group') if name in e} for e in result['kms']]
    return result


def update(kms, doc, key, pick, text=None, rotate=False, write_pick=None):
    data_key, old_text = read(kms, doc, key, pick)
    result = known_fields(doc)
    write_pick = pick if write_pick is None else write_pick
    if rotate:
        data_key = os.urandom(32)
        parts = split(data_key, sorted({group(e) for e in result['kms']}), threshold(doc))
        for entry in result['kms']:
            entry['enc'] = b64(kms.encrypt(KeyId=entry['arn'], Plaintext=material(doc, entry, data_key, parts),
                             EncryptionContext=request_context(entry['context'], write_pick))['CiphertextBlob'])
            entry['created_at'] = now()
    elif tuple(map(int, result['sops_version'].split('.'))) < (3, 2, 0):
        result['sops_version'] = '3.2.0'
    result['payload'] = seal(data_key, old_text if text is None else text, key)
    result['lastmodified'] = now()
    return result


def main():
    parser = argparse.ArgumentParser(description='Deployed client: unconditional edit/rotate; deterministic request ordering.')
    parser.add_argument('operation', choices=['decrypt', 'edit', 'rotate'])
    parser.add_argument('--bucket', required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--pick', type=int, default=0)
    parser.add_argument('--text')
    parser.add_argument('--write-pick', type=int, help='request ordering for rotate Encrypt calls; defaults to --pick')
    args = parser.parse_args()
    s3, kms = client('s3'), client('kms')
    doc, _ = load(s3, args.bucket, args.key)
    if args.operation == 'decrypt':
        print(read(kms, doc, args.key, args.pick)[1])
    else:
        result = update(kms, doc, args.key, args.pick, args.text, args.operation == 'rotate', args.write_pick)
        s3.put_object(Bucket=args.bucket, Key=args.key, Body=encode(result))


if __name__ == '__main__':
    main()

import copy
from shares import group, threshold, combine, split, material
from codec import un64, open_payload, now
import keyservice


def affected(doc):
    return tuple(map(int, doc['sops_version'].split('.'))) < (3, 3, 0) and any(
        len(e['context']) >= 2 and len(set(e['context'].values())) > 1 for e in doc['kms'])


def recover(kms, doc, key):
    found = {}
    for entry in doc['kms']:
        g = group(entry)
        if g in found:
            continue
        try:
            part = keyservice.decrypt(kms, entry)
            found[g] = part
        except Exception:
            continue
    if threshold(doc) == 1:
        for part in found.values():
            try:
                open_payload(doc, part, key)
                return part
            except Exception:
                continue
    elif len(found) >= threshold(doc):
        data_key = combine(list(found.values())[:threshold(doc)])
        open_payload(doc, data_key, key)
        return data_key
    if affected(doc):
        entry = next(e for e in doc['kms'] if len(set(e['context'].values())) > 1)
        for value in set(entry['context'].values()):
            try:
                data_key = kms.decrypt(KeyId=entry['arn'], CiphertextBlob=un64(entry['enc']),
                                       EncryptionContext={k: value for k in entry['context']})['Plaintext']
                open_payload(doc, data_key, key)
                return data_key
            except Exception:
                continue
    raise ValueError('Failed to decrypt document')


def candidate(kms, doc, key, retired):
    if not affected(doc):
        return None
    data_key = recover(kms, doc, key)
    result = copy.deepcopy(doc)
    parts = split(data_key, sorted({group(e) for e in doc['kms']}), threshold(doc))
    for entry in result['kms']:
        entry['enc'] = keyservice.encrypt(kms, entry, material(doc, entry, data_key, parts))
        entry['created_at'] = now()
    result['sops_version'] = '3.3.0'
    return result

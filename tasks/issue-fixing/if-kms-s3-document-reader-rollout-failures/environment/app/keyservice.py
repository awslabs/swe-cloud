from codec import b64, un64


def request_context(context, pick=0):
    names = sorted(context)
    if not names:
        return {}
    offset = pick % len(names)
    names = names[offset + 1:] + names[:offset + 1]
    cell = [None]
    values = {}
    for name in names:
        cell[0] = context[name]
        values[name] = cell
    return {name: value[0] for name, value in values.items()}


def encrypt(kms, entry, data_key):
    return b64(kms.encrypt(KeyId=entry['arn'], Plaintext=data_key,
                           EncryptionContext=request_context(entry['context']))['CiphertextBlob'])


def decrypt(kms, entry):
    return kms.decrypt(KeyId=entry['arn'], CiphertextBlob=un64(entry['enc']),
                       EncryptionContext=request_context(entry['context']))['Plaintext']

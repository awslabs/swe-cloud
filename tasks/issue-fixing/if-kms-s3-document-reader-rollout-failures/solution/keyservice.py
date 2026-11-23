from codec import b64, un64


def request_context(context, pick=0):
    return dict(context)


def encrypt(kms, entry, data_key):
    return b64(kms.encrypt(KeyId=entry['arn'], Plaintext=data_key,
                           EncryptionContext=dict(entry['context']))['CiphertextBlob'])


def decrypt(kms, entry):
    return kms.decrypt(KeyId=entry['arn'], CiphertextBlob=un64(entry['enc']),
                       EncryptionContext=dict(entry['context']))['Plaintext']

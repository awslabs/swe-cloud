import os
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Envelope:
    def __init__(self, kms, key_id):
        self.kms = kms
        self.key_id = key_id
        self.current_key_id = kms.describe_key(KeyId=key_id)["KeyMetadata"]["KeyId"]

    def encrypt(self, value):
        dek, iv = os.urandom(32), os.urandom(12)
        wrapped = self.kms.encrypt(KeyId=self.key_id, Plaintext=dek)
        self.current_key_id = wrapped["KeyId"]
        return dict(key_arn=wrapped["KeyId"], wrapped_dek=wrapped["CiphertextBlob"],
                    iv=iv, ct=AESGCM(dek).encrypt(iv, value, None))

    def decrypt(self, item):
        response = self.kms.decrypt(CiphertextBlob=item["wrapped_dek"])
        return AESGCM(response["Plaintext"]).decrypt(item["iv"], item["ct"], None)

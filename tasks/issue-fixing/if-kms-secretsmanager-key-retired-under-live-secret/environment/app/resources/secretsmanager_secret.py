"""SecretsManagerSecret.

Sandbox secrets created with a customer managed key (`KmsKeyId`) hold a client-side envelope:
SecretString is the base64 of KMS ciphertext produced under that key (see README). Secrets on the
AWS managed key hold their value as is.
"""
import base64

from libnuke.registry import Registration

from .aws import client
from .backup import decrypt_envelope, write_backup

TYPE = "SecretsManagerSecret"


class SecretsManagerSecret:
    def __init__(self, name, arn, kms_key_id, tags, settings):
        self.name = name
        self.arn = arn
        self.kms_key_id = kms_key_id
        self.tags = tags
        self.settings = settings

    def string(self):
        return self.name

    def properties(self):
        props = {"Name": self.name, "ARN": self.arn, "KmsKeyId": self.kms_key_id or ""}
        for tag in self.tags:
            props[f"tag:{tag['Key']}"] = tag.get("Value", "")
        return props

    def remove(self):
        sm = client("secretsmanager")
        value = sm.get_secret_value(SecretId=self.arn)
        raw = value.get("SecretString")
        if raw is None:
            raw_bytes = value["SecretBinary"]
        else:
            raw_bytes = raw.encode()
        if self.kms_key_id:
            plaintext, key_arn = decrypt_envelope(base64.b64decode(raw_bytes), self.name)
        else:
            plaintext, key_arn = raw_bytes, None
        write_backup(self.settings, TYPE, self.name, plaintext, key_arn)
        sm.delete_secret(SecretId=self.arn, ForceDeleteWithoutRecovery=True)


class SecretsManagerSecretLister:
    def list(self, settings):
        sm = client("secretsmanager")
        resources = []
        for page in sm.get_paginator("list_secrets").paginate():
            for s in page.get("SecretList", []):
                resources.append(SecretsManagerSecret(s["Name"], s["ARN"], s.get("KmsKeyId"),
                                                      s.get("Tags", []), settings))
        return resources


REGISTRATION = Registration(TYPE, SecretsManagerSecretLister())

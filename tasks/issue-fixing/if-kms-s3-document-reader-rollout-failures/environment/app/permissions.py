"""Local endpoint permission tier for imported, retired wrapping keys.

The inventory is deployment policy, not a document migration checkpoint. Production
uses KMS IAM policy; the emulator gateway models the same denied destinations.
"""
from botocore.exceptions import ClientError


class KMSPrincipal:
    def __init__(self, kms, inventory):
        self.kms = kms
        self.decrypt_only = frozenset(inventory.get('decrypt_only', []))

    def __getattr__(self, name):
        call = getattr(self.kms, name)
        if name not in ('encrypt', 're_encrypt', 'generate_data_key',
                        'generate_data_key_without_plaintext'):
            return call

        def permitted(**kwargs):
            arn = kwargs.get('DestinationKeyId') if name == 're_encrypt' else kwargs.get('KeyId')
            if arn in self.decrypt_only:
                raise ClientError({'Error': {'Code': 'AccessDeniedException',
                                   'Message': 'rollout principal has Decrypt only on ' + arn},
                                   'ResponseMetadata': {'HTTPStatusCode': 400}}, name)
            return call(**kwargs)
        return permitted

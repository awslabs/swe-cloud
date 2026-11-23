"""Resource types of the sandbox sweep.

Registration order is processing order: consumers are queued before the keys they are encrypted
under, so within a pass a consumer is backed up and deleted before its key is reached.
"""
from libnuke import registry

from . import s3_envelope_object, secretsmanager_secret, kms_key

for _module in (s3_envelope_object, secretsmanager_secret, kms_key):
    registry.register(_module.REGISTRATION)

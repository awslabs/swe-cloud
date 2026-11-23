import base64
import os
from datetime import datetime, timezone
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def b64(raw):
    return base64.b64encode(raw).decode()


def un64(value):
    return base64.b64decode(value)


def now():
    return datetime.now(timezone.utc).isoformat()


def seal(data_key, text, object_key):
    nonce = os.urandom(12)
    return {'nonce': b64(nonce), 'ciphertext': b64(AESGCM(data_key).encrypt(nonce, text.encode(), object_key.encode()))}


def open_payload(doc, data_key, object_key):
    p = doc['payload']
    return AESGCM(data_key).decrypt(un64(p['nonce']), un64(p['ciphertext']), object_key.encode()).decode()

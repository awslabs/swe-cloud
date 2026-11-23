#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3) solver/verifier images.
# The reference itself is a static ELF and needs no runtime libraries.
# Pinned general-purpose supporting readers (not the subject's work):
#   PyYAML       - parse the secrets YAML file for inspection
#   cryptography - independent AES-256-GCM decryption of stored ciphertexts
#   PyNaCl       - independent NaCl secretbox (XSalsa20-Poly1305) decryption
set -euo pipefail
python3 -m pip install --no-cache-dir --disable-pip-version-check \
  'PyYAML==6.0.2' 'cryptography==43.0.3' 'PyNaCl==1.5.0'
python3 -c 'import yaml, nacl.secret, cryptography; print("runtime ok")'

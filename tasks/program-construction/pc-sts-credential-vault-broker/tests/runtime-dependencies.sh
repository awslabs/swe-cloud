#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3). The reference is a static ELF and
# needs no libraries. Installed here as general-purpose supporting infrastructure for
# the solver/verifier: a pinned JOSE (RFC 7516 JWE) + crypto library, because the
# encrypted file keyring stores each item as a compact JWE (PBES2-HS256+A128KW /
# A256GCM). No upstream binary or alias is installed.
set -euo pipefail
python3 -m pip install --no-cache-dir --disable-pip-version-check --only-binary=:all: \
  cryptography==46.0.3 cffi==2.0.0 pycparser==2.23 jwcrypto==1.5.6 typing_extensions==4.15.0
python3 -c 'import jwcrypto.jwe, cryptography, boto3; print("runtime ok")'

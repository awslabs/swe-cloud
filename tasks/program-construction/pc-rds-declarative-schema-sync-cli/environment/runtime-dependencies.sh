#!/usr/bin/env bash
# Runtime image setup on python:3.12-bookworm (boto3 preinstalled).
# The reference ELF is static and needs no system libraries. These pinned
# general-purpose packages are supporting infrastructure for observing state:
#  - psycopg (binary wheel): inspect the Postgres catalog after apply.
#  - python-hcl2: parse generic HCL2 syntax (e.g. `schema inspect` output) structurally.
set -euo pipefail
python3 -m pip install --no-cache-dir --disable-pip-version-check \
  'psycopg[binary]==3.2.10' 'python-hcl2==7.3.1'
python3 -c 'import psycopg, hcl2, boto3; print("psycopg", psycopg.__version__)'

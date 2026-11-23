#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3). The reference is a static Go ELF and
# needs no runtime libraries. psycopg is installed as general-purpose infrastructure:
# a pinned PostgreSQL client library for fixtures/verification and for the solver
# (speaking the Postgres wire protocol is supporting infrastructure, not task work).
set -euo pipefail
python3 -m pip install --no-cache-dir --disable-pip-version-check \
  "psycopg[binary]==3.2.10" "psycopg-binary==3.2.10"
python3 -c 'import psycopg; print("psycopg", psycopg.__version__)'

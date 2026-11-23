#!/usr/bin/env bash
# Runtime/verifier/solver image setup on python:3.12-bookworm (boto3 preinstalled).
# The reference is a static Go ELF and needs no system libraries. psycopg is installed
# as general-purpose infrastructure: the program's state lives in RDS Postgres, and
# tests (and a reimplementation) need a Postgres client; the Postgres wire protocol is
# not intended task work.
set -euo pipefail
python3 -m pip install --no-cache-dir 'psycopg[binary]==3.3.9'
python3 -c 'import psycopg; print("psycopg", psycopg.__version__)'

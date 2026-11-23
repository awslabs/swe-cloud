#!/usr/bin/env bash
# Runtime setup for the python:3.12-bookworm solver/verifier images.
# The reference is a static CGO-free Go ELF: it needs no shared libraries.
# Admitted infrastructure: a pinned PostgreSQL client library (psycopg 3 with
# bundled libpq) so fixtures and solvers can inspect the bookkeeping table and
# user tables in RDS Postgres. boto3 is already present for S3/RDS setup.
set -euo pipefail
python3 -m pip install --no-cache-dir 'psycopg[binary]==3.2.10'
python3 -c 'import psycopg, boto3; print("psycopg", psycopg.__version__)'

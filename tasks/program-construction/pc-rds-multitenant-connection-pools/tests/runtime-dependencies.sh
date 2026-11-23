#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3), used for the probe, solver and verifier images.
# The reference binary itself only needs glibc/libgcc/libm (already in the base image).
# Admitted general-purpose PostgreSQL client tooling (supporting infrastructure, not task work):
#   * psql from Debian bookworm's postgresql-client-15 (simple-query protocol by default);
#   * psycopg 3 (pinned, binary wheel bundling libpq) for Python clients.
# Neither is part of the reference; no upstream binary, source or alias is installed.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends postgresql-client-15
rm -rf /var/lib/apt/lists/*
python3 -m pip install --no-cache-dir 'psycopg[binary]==3.2.3'
psql --version
python3 -c 'import psycopg; print("psycopg", psycopg.__version__, psycopg.pq.__impl__)'

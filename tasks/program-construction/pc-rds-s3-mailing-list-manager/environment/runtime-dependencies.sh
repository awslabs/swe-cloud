#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3). The reference itself is a
# static Go ELF with embedded assets and needs no system libraries.
# Installed general-purpose infrastructure (not task work):
#  - psycopg 3 (binary): PostgreSQL client used to inspect graded table state
#    and available to a reconstruction for talking to RDS Postgres.
#  - Pillow: generic image codec, so PNG/JPEG decoding/resizing for media
#    thumbnails is infrastructure rather than something to hand-write.
#  - postgresql-client (psql): ad-hoc inspection of the RDS database.
set -euo pipefail
python3 -m pip install --no-cache-dir 'psycopg[binary]==3.2.10' 'pillow==11.3.0'
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends postgresql-client >/dev/null
  rm -rf /var/lib/apt/lists/*
fi
python3 -c 'import psycopg, PIL, boto3; print("psycopg", psycopg.__version__, "pillow", PIL.__version__)'

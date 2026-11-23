#!/usr/bin/env bash
# Runtime setup for the event-destination-hub reference, applied to a fresh
# python:3.12-bookworm image (boto3 preinstalled). The reference ELF is static
# (CGO_ENABLED=0) and needs no system libraries. The program's state lives in
# Valkey and PostgreSQL engines that Floci starts; these pinned, general-purpose
# clients let probes/verifiers (and the solver) inspect that state directly.
# No upstream binary or source is installed here.
set -euo pipefail
python3 -m pip install --no-cache-dir --disable-pip-version-check \
  'redis==8.1.0' 'psycopg[binary]==3.3.6'
python3 -c 'import redis, psycopg, boto3; print("runtime ok", redis.__version__, psycopg.__version__, boto3.__version__)'

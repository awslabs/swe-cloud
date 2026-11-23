#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3). The reference program is a
# static ELF with no shared-library needs. Installed here are only general-purpose,
# pinned supporting tools: redis-py (RESP client used to seed/inspect Valkey
# fixtures) and prometheus-client (Prometheus text exposition parser used to read
# /metrics output). No upstream binary or source is installed.
set -euo pipefail
python3 -m pip install --no-cache-dir --disable-pip-version-check \
  'redis==8.1.0' 'prometheus-client==0.26.0'
python3 -c 'import redis, prometheus_client; print("redis", redis.__version__)'

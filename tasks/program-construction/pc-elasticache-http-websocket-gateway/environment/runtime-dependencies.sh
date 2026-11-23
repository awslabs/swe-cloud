#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3), used by solver and verifier images.
# The reference ELF is self-contained apart from glibc, so no system packages are needed.
# Pinned general-purpose client libraries (supporting infrastructure, not the subject):
#   redis            - inspect/seed Valkey state independently of the HTTP gateway
#   msgpack          - decode the public MessagePack format returned by `.msg` URLs
#   websocket-client - RFC 6455 client for the WebSocket endpoints
set -euo pipefail
python3 -m pip install --no-cache-dir --disable-pip-version-check \
  'redis==5.2.1' 'msgpack==1.1.0' 'websocket-client==1.8.0'
python3 -c 'import redis, msgpack, websocket; print("runtime ok", redis.__version__, msgpack.version, websocket.__version__)'

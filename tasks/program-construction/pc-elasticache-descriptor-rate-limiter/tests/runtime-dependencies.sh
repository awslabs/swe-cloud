#!/usr/bin/env bash
# Runtime/verification tooling for the python:3.12-bookworm image.
# The reference itself is a static ELF with no runtime library needs.
# General-purpose, pinned client infrastructure (not the subject's logic):
#   redis    - RESP client for inspecting counter keys/TTLs in Valkey
#   grpcio, grpcio-tools, protobuf - gRPC client + protoc for calling the
#              public ShouldRateLimit RPC (see the interface guide's proto subset)
set -euo pipefail
python3 -m pip install --no-cache-dir \
  redis==5.2.1 \
  grpcio==1.68.1 \
  grpcio-tools==1.68.1 \
  protobuf==5.29.3
python3 -c 'import redis, grpc, grpc_tools, google.protobuf; print("runtime tools ok")'

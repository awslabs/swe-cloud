#!/usr/bin/env bash
# Runtime setup for a fresh python:3.12-bookworm image (with boto3); network is
# available here. The same recipe goes into the solver and verifier images.
#
# The reference payload (`program`) is a dynamically linked glibc ELF needing
# only libc/libm/libgcc_s (present in the base image). It speaks HTTP to the
# Floci endpoint (no TLS trust store needed for http://).
#
# Supporting infrastructure (not intended task work): OTLP is a standard wire
# format (protobuf over HTTP and gRPC). The pinned, general-purpose
# OpenTelemetry protobuf bindings and gRPC runtime are installed so drivers,
# tests and reconstructions can encode and decode OTLP metrics requests without
# hand-writing protobuf codecs. They are libraries only; no upstream binary is installed.
set -euxo pipefail

python3 -m pip install --no-cache-dir \
  'opentelemetry-proto==1.45.1' \
  'protobuf==7.36.2' \
  'grpcio==1.85.0'

python3 -c 'import grpc, google.protobuf, opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 as m; print("otlp bindings ok", grpc.__version__)'

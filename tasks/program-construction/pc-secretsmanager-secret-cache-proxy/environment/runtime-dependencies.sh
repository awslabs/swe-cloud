#!/usr/bin/env bash
# Runtime setup for the python:3.12-bookworm solver/verifier images.
# The reference payload is a fully static (musl, static-PIE) executable: it needs no
# shared libraries, interpreters or data files. Its only external inputs are a TOML
# config file, environment variables and the Secrets Manager/STS APIs, all plain
# JSON/HTTP that python3 + boto3 (already in the base image) can produce and observe.
# No admitted binary data format needs a third-party reader, so nothing is installed.
set -euo pipefail
python3 -c "import boto3, json, urllib.request, tomllib" >/dev/null

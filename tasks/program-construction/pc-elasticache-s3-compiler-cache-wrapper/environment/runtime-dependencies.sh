#!/usr/bin/env bash
# Runtime setup for the python:3.12-bookworm solver/verifier images.
#  * gcc/libc6-dev: the compiler the cache wraps (already in the image; ensured here).
#  * redis-py: client used to inspect Redis/Valkey cache entries and TTLs.
#  * zstd/libzstd-dev + python zstandard: general-purpose codec support. Cache entries
#    are zip archives whose members are zstd frames; compressing/decoding them is
#    supporting infrastructure, not intended task work.
# No upstream binary or source is installed.
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends gcc libc6-dev zstd libzstd-dev
rm -rf /var/lib/apt/lists/*
python3 -m pip install --no-cache-dir 'redis==5.2.1' 'zstandard==0.23.0'

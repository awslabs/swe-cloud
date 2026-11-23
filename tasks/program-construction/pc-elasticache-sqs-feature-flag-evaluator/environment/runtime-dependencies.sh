#!/usr/bin/env bash
# Runtime setup for python:3.12-bookworm (+boto3). The reference itself is a static
# ELF with no library dependencies. Installed here are pinned general-purpose
# infrastructure clients used by probes/tests (and available to the solver):
#  - redis-py: write/modify flag keys in the ElastiCache (Valkey) engine
#  - PyYAML:   read/write the YAML relay configuration format (supporting infrastructure)
set -euo pipefail
python3 -m pip install --no-cache-dir 'redis==5.2.1' 'PyYAML==6.0.2'
python3 -c 'import redis, yaml, boto3; print("redis", redis.__version__, "yaml", yaml.__version__)'

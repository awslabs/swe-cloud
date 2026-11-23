#!/usr/bin/env bash
# Runtime image setup on python:3.12-bookworm (+boto3). The reference program is a
# static Go ELF with no shared-library needs (Floci is reached over plain
# http). Installs only general-purpose infrastructure:
#   - psycopg 3 (binary wheel): Postgres client used to provision databases and to
#     inspect stored rows; also usable by a reconstruction.
# The KPL aggregated-record framing on Kinesis is intended task work and is
# documented publicly instead of being provided by a library.
set -euo pipefail
python3 -m pip install --no-cache-dir 'psycopg[binary]==3.2.10'

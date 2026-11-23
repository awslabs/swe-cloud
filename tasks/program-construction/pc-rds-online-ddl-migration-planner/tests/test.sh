#!/usr/bin/env bash
set -euo pipefail
exec env -i PATH=/usr/local/bin:/usr/bin:/bin HOME=/root LANG=C.UTF-8 \
  AWS_ENDPOINT_URL=http://engines:4566 AWS_DEFAULT_REGION=us-east-1 AWS_REGION=us-east-1 AWS_ACCESS_KEY_ID=test AWS_SECRET_ACCESS_KEY=test AWS_EC2_METADATA_DISABLED=true \
  PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -I /tests/verify.py

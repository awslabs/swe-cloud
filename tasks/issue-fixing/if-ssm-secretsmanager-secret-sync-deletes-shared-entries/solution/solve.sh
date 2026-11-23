#!/usr/bin/env bash
# Install the reference destinations into /app/syncctl.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/syncctl/parameter_store.py" /app/syncctl/parameter_store.py
cp "$here/syncctl/secrets_manager.py" /app/syncctl/secrets_manager.py
cp "$here/syncctl/tags.py" /app/syncctl/tags.py
cp "$here/syncctl/aws.py" /app/syncctl/aws.py

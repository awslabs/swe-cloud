#!/usr/bin/env bash
# Reference: per-key live in-use gate (fail closed) + give-up rule that counts held items as stuck.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/app/resources/consumers.py" /app/resources/consumers.py
cp "$here/app/resources/kms_key.py" /app/resources/kms_key.py
cp "$here/app/libnuke/engine.py" /app/libnuke/engine.py

#!/usr/bin/env bash
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here"/refresher/unique.py "$here"/refresher/handler.py /app/refresher/

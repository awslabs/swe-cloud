#!/usr/bin/env bash
# Install the reference handlers into /app/crossref.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/crossref/ledger.py" "$here/crossref/writer.py" "$here/crossref/reader.py" /app/crossref/

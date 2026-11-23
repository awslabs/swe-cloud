#!/usr/bin/env bash
set -euo pipefail
solution_dir="$(dirname "$0")"
cp "$solution_dir/worker.py" /app/worker.py
cp "$solution_dir/group.py" /app/group.py
cp "$solution_dir/pipeline.py" /app/pipeline.py

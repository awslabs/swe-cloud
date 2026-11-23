#!/usr/bin/env bash
# Reference: idempotent lease re-acquisition under the caller's own lease ID, releases conditional on
# the lease the caller holds, an in-script expiry re-check for scavenging, and an executor that keeps
# every queue time on the shard's clock (not its host's), learns
# the outcome of an ambiguous lease call and waits for its in-flight extension before releasing; the step
# call is keyed by a run ID kept in the item (q_start), re-attached on any non-answer, cleared on requeue.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/stepq/library.lua" /app/stepq/library.lua
cp "$here/stepq/executor.py" /app/stepq/executor.py

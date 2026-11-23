#!/usr/bin/env bash
# Reference: the add decides "duplicate" from the owner it names (throttle windows aside) and drops
# data kept for a gone owner; keep-last parks while a handler holds the lock; every finalization
# (including the stall-limit failure and a job taken out of active by hand) releases the dedup id
# and creates the kept job.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here/lua/addJob.lua" /app/dq/lua/addJob.lua
cp "$here/lua/moveToFinished.lua" /app/dq/lua/moveToFinished.lua
cp "$here/lua/moveStalledJobsToWait.lua" /app/dq/lua/moveStalledJobsToWait.lua

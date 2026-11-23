#!/usr/bin/env bash
# Install the reference: a release that gives the lock back only with its own removal of the
# in-progress entry, retried until answered; a fetch that returns claims whose reply was lost; and a
# shim per job that holds the claim's lease while the runner lives and releases it when the runner
# exits, so a runner that outlives its pool process keeps its unit and its job is not run again.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
cp "$here"/work/*.py /app/work/

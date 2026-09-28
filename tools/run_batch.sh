#!/bin/sh
# Start tools/batch_translate.py as a one-off container on the Jellyfin host (run from the repo root).
# Needs batch.env (not in git) with JF_KEY=... and DS_KEY=...; extra docker args are passed through,
# e.g.  tools/run_batch.sh -e START_AT=18:00      or      tools/run_batch.sh -e DRY_RUN=1
# Follow progress with: docker logs -f metatube-batch
cd "$(dirname "$0")/.." || exit 1
docker rm -f metatube-batch >/dev/null 2>&1
exec docker run -d --name metatube-batch --network host --env-file batch.env -v "$PWD/data:/data" "$@" \
  metatube-deepseek-proxy:local python tools/batch_translate.py

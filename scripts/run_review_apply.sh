#!/usr/bin/env bash
# Opens a browser and walks through applications above a score threshold,
# one at a time, stopping for your confirmation before each submit.
# NOT meant for unattended/cron use — it needs you at the keyboard.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
jobbot batch --min-score "${MIN_SCORE:-75}" --limit "${LIMIT:-10}"

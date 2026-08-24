#!/usr/bin/env bash
# Pulls new postings and scores them. Safe to run on a cron/launchd timer —
# it never opens a browser or touches any application form.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
jobbot discover
jobbot match

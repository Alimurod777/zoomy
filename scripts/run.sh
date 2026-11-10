#!/usr/bin/env bash
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/runtime-env.sh"
cd "$ZOOMY_ROOT"
exec "$ZOOMY_ROOT/venv/bin/python" "$ZOOMY_ROOT/scripts/start-bot.py"

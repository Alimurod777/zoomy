#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
mkdir -p .runtime/bin
curl -fsSL --retry 2 --max-time 120 -o .runtime/bin/cloudflared \
    https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-linux-amd64
printf '%s  %s\n' \
    1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872 \
    .runtime/bin/cloudflared | sha256sum -c -
chmod +x .runtime/bin/cloudflared
venv/bin/python -m pip install 'aiohttp>=3.11,<4'
.runtime/bin/cloudflared --version

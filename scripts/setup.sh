#!/usr/bin/env bash
# One-command install/update without root or Docker. Safe to re-run from any directory:
#   bash scripts/setup.sh
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"

say() { printf '\n==> %s\n' "$1"; }
die() { printf '\nXATO: %s\n' "$1" >&2; exit 1; }

[[ -f bot/requirements.txt ]] || die "bot/requirements.txt topilmadi ($root).
Loyiha to'liq yuklanmagan: git clone https://github.com/Alimurod777/zoomy.git"

if [[ ! -x venv/bin/python ]]; then
    # alwaysdata exposes the version chosen in Environment > Python as `python`.
    py=""
    for candidate in ${PYTHON:-} python python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' 2>/dev/null; then
            py=$candidate
            break
        fi
    done
    [[ -n "$py" ]] || die "Python 3.10 yoki yangiroq kerak.
alwaysdata: panel > Environment > Python da 3.12 ni tanlang yoki: PYTHON_VERSION=3.12 bash scripts/setup.sh"
    say "venv yaratilmoqda ($("$py" --version 2>&1))"
    "$py" -m venv venv
fi

say "Python kutubxonalari (bot/requirements.txt)"
venv/bin/python -m pip install --no-cache-dir --quiet --upgrade pip
venv/bin/python -m pip install --no-cache-dir -r bot/requirements.txt

say "Chromium (Playwright)"
venv/bin/python -m playwright install chromium

if [[ ! -x .runtime/root/usr/bin/ffmpeg ]]; then
    say "FFmpeg, Xvfb, PulseAudio (.runtime ichiga, root kerak emas)"
    bash scripts/setup-runtime.sh
fi

if [[ ! -x .runtime/bin/cloudflared ]]; then
    say "Zoom login havolasi uchun cloudflared"
    bash scripts/setup-login-link.sh
fi

if [[ ! -f .env ]]; then
    cp .env.example .env
    chmod 600 .env
    say ".env yaratildi (namunadan)"
fi

say "Tekshiruv"
venv/bin/python -m pip check

cat <<EOF

Tayyor. Keyingi qadamlar:

1) .env ni to'ldiring (TELEGRAM_TOKEN, AUTHORIZED_USER_IDS, OWNER_TELEGRAM_ID):
     nano "$root/.env"

2) Sinov (to'xtatish: Ctrl+C):
     bash "$root/scripts/run.sh"

3) alwaysdata panel > Advanced > Services > Command:
     /bin/bash $root/scripts/run.sh

Ixtiyoriy: bash scripts/setup-mongodb.sh (lokal MongoDB),
           bash scripts/setup-zoom-desktop.sh (native Zoom Desktop).
EOF

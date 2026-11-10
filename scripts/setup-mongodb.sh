#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
mkdir -p .runtime/mongodb
if [[ ! -x .runtime/mongodb/bin/mongod ]]; then
    curl -fsSL --retry 2 --max-time 180 -o .runtime/mongodb/server.tgz \
        https://fastdl.mongodb.org/linux/mongodb-linux-x86_64-debian12-8.0.30.tgz
    printf '%s  %s\n' \
        88ea17afe3ecfbcc766ddb33fe34e5ee54964c38f5069682553e20e73530a48c \
        .runtime/mongodb/server.tgz | sha256sum -c -
    tar -xzf .runtime/mongodb/server.tgz -C .runtime/mongodb --strip-components=1
fi
exec venv/bin/python scripts/local-mongo.py init

#!/usr/bin/env bash
# Source from an interactive shell or from run.sh; no root or Docker needed.
ZOOMY_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export ZOOMY_ROOT
export PATH="$ZOOMY_ROOT/.runtime/bin:$ZOOMY_ROOT/.runtime/root/usr/bin:$ZOOMY_ROOT/venv/bin:$PATH"
export LD_LIBRARY_PATH="$ZOOMY_ROOT/.runtime/root/usr/lib/x86_64-linux-gnu:$ZOOMY_ROOT/.runtime/root/usr/lib/x86_64-linux-gnu/pulseaudio:$ZOOMY_ROOT/.runtime/root/usr/lib/pulse-16.1+dfsg1/modules${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PULSE_RUNTIME_PATH="/tmp/zoomy-$(id -u)/pulse"
export PULSE_SERVER="unix:$PULSE_RUNTIME_PATH/native"
if [[ -f "$ZOOMY_ROOT/.runtime/pulse-client.conf" ]]; then
    export PULSE_CLIENTCONFIG="$ZOOMY_ROOT/.runtime/pulse-client.conf"
fi
if [[ -d "$ZOOMY_ROOT/.runtime/root/usr/share/X11/xkb" ]]; then
    export XKB_CONFIG_ROOT="$ZOOMY_ROOT/.runtime/root/usr/share/X11/xkb"
fi
export TMPDIR=/tmp
mkdir -p "$PULSE_RUNTIME_PATH"
chmod 700 "/tmp/zoomy-$(id -u)" "$PULSE_RUNTIME_PATH"

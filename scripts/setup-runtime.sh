#!/usr/bin/env bash
# Debian 12 amd64: extract official packages into this project, never install as root.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
mkdir -p "$root/.runtime/debs" "$root/.runtime/root" "$root/.runtime/bin"
cat > "$root/.runtime/sources.list" <<'EOF'
deb https://deb.debian.org/debian bookworm main
deb https://deb.debian.org/debian-security bookworm-security main
deb https://deb.debian.org/debian bookworm-updates main
EOF
apt_options=(-o "Dir::Etc::sourcelist=$root/.runtime/sources.list"
    -o Dir::Etc::sourceparts=- -o Dir::Etc::main=/dev/null -o Dir::Etc::parts=-
    -o "APT::Sandbox::User=$(id -un)")
cd "$root/.runtime/debs"
# APT verifies downloads against the host's signed Debian package index.
apt-get "${apt_options[@]}" download \
    ffmpeg xvfb pulseaudio pulseaudio-utils x11-xkb-utils x11vnc novnc \
    libavdevice59 libavfilter8 libavformat59 libavcodec59 libswresample4 \
    libswscale6 libavutil57 libpostproc56 libxfont2 libwebrtc-audio-processing1 \
    libspeexdsp1 libpulsedsp libvncserver1 libvncclient1 libtdb1 \
    libopenal1 libcdio-paranoia2 libcdio-cdda2 libdc1394-25 libsdl2-2.0-0 \
    libxv1 libpocketsphinx3 libsphinxbase3 libbs2b0 liblilv-0-0 librubberband2 \
    libmysofa1 libplacebo208 libass9 libva2 libvidstab1.1 libzimg2 libmfx1 \
    libgme0 libopenmpt0 libchromaprint1 libbluray2 librist4 libsrt1.5-gnutls \
    libssh-gcrypt-4 libzvbi0 libcodec2-1.0 libgsm1 libshine3 libxvidcore4 \
    libsoxr0 libva-drm2 libva-x11-2 libvdpau1 libsndio7.0 libcdio19 \
    libusb-1.0-0 libdecor-0-0 libserd-0-0 libsord-0-0 libsratom-0-0 \
    libvorbisfile3 libudfread0 libcjson1 libxkbfile1
for deb in ./*.deb; do dpkg-deb -x "$deb" "$root/.runtime/root"; done
# Debian Xvfb hardcodes /usr/bin for xkbcomp. Change that one string in our
# extracted copy to '.' (same byte count), then run it from its private bin dir.
python3 - "$root/.runtime/root/usr/bin/Xvfb" <<'PY'
from pathlib import Path
import sys
p = Path(sys.argv[1])
data = p.read_bytes()
assert data.count(b'/usr/bin\0') == 1, 'Unexpected Xvfb binary layout'
p.write_bytes(data.replace(b'/usr/bin\0', b'.\0' + b'\0' * 7))
PY
cat > "$root/.runtime/bin/Xvfb" <<'EOF'
#!/usr/bin/env bash
set -e
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root/.runtime/root/usr/bin"
exec ./Xvfb "$@"
EOF
chmod +x "$root/.runtime/bin/Xvfb"
cat > "$root/.runtime/pulse.pa" <<EOF
.fail
load-module module-native-protocol-unix socket=/tmp/zoomy-$(id -u)/pulse/native
load-module module-null-sink sink_name=zoomy_base
set-default-sink zoomy_base
EOF
cat > "$root/.runtime/pulse-client.conf" <<EOF
default-server = unix:/tmp/zoomy-$(id -u)/pulse/native
autospawn = no
EOF
cat > "$root/.runtime/bin/pulseaudio" <<'EOF'
#!/usr/bin/env bash
set -e
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
args=()
for arg in "$@"; do
    if [[ "$arg" == --start ]]; then
        # --start self-executes /usr/bin/pulseaudio, absent on shared hosting.
        args+=(--daemonize=yes)
    else
        args+=("$arg")
    fi
done
exec "$root/.runtime/root/usr/bin/pulseaudio" -n \
    --file="$root/.runtime/pulse.pa" \
    --dl-search-path="$root/.runtime/root/usr/lib/pulse-16.1+dfsg1/modules" "${args[@]}"
EOF
chmod +x "$root/.runtime/bin/pulseaudio"
source "$root/scripts/runtime-env.sh"
for binary in ffmpeg ffprobe Xvfb xkbcomp pulseaudio pactl parec x11vnc; do
    missing=$(ldd "$root/.runtime/root/usr/bin/$binary" | sed -n '/not found/p')
    if [[ -n "$missing" ]]; then printf '%s: %s\n' "$binary" "$missing" >&2; exit 1; fi
done
ffmpeg -version | head -1
pactl info >/dev/null 2>&1 || pulseaudio --start --exit-idle-time=-1 --log-level=warning
pactl info

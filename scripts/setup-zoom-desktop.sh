#!/usr/bin/env bash
# Official Linux client, extracted into this project without sudo.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
version=7.2.5.6661
checksum=5cd7b9f1e6dc7226a44193df23d0664790496e8267f26152b0cbbd5888025444
mkdir -p "$root/.runtime/downloads" "$root/.runtime/zoom" "$root/.runtime/debs"
package="$root/.runtime/downloads/zoom_amd64.deb"
if ! [[ -f "$package" ]] || ! echo "$checksum  $package" | sha256sum --check --status; then
    curl -fL --proto '=https' --connect-timeout 15 --max-time 300 \
        "https://cdn.zoom.us/prod/$version/zoom_amd64.deb" -o "$package"
fi
echo "$checksum  $package" | sha256sum --check --status
dpkg-deb -x "$package" "$root/.runtime/zoom"
cd "$root/.runtime/debs"
apt-get -o "Dir::Etc::sourcelist=$root/.runtime/sources.list" \
    -o Dir::Etc::sourceparts=- -o Dir::Etc::main=/dev/null -o Dir::Etc::parts=- download \
    libxcb-cursor0/bookworm libxcb-xtest0/bookworm libxcb-keysyms1/bookworm \
    libxcb-icccm4/bookworm libxcb-image0/bookworm libxcb-util1/bookworm \
    libxcb-render-util0/bookworm libxcb-xinerama0/bookworm libxkbcommon-x11-0/bookworm \
    libxcb-xkb1/bookworm xdotool/bookworm libxdo3/bookworm openbox/bookworm wmctrl/bookworm \
    libobt2v5/bookworm libobrender32v5/bookworm libstartup-notification0/bookworm libimlib2/bookworm
for deb in *xcb*.deb *xdo*.deb *openbox*.deb *wmctrl*.deb *libobt*.deb *libobrender*.deb *startup*.deb *libimlib2*.deb; do
    dpkg-deb -x "$deb" "$root/.runtime/root"
done
# ZoomLauncher resets LD_LIBRARY_PATH. Make our private dependencies visible
# beside the client without replacing any system libraries.
for name in libxcb-cursor.so.0 libxcb-xtest.so.0 libxcb-keysyms.so.1 \
    libxcb-icccm.so.4 libxcb-image.so.0 libxcb-util.so.1 libxcb-render-util.so.0 \
    libxcb-xinerama.so.0 libxkbcommon-x11.so.0 libxcb-xkb.so.1; do
    lib="$root/.runtime/root/usr/lib/x86_64-linux-gnu/$name"
    name=$(basename "$lib")
    if ! [[ -e "$root/.runtime/zoom/opt/zoom/$name" ]]; then
        ln -s "$lib" "$root/.runtime/zoom/opt/zoom/$name"
    fi
done
source "$root/scripts/runtime-env.sh"
missing=$(LD_LIBRARY_PATH="$root/.runtime/zoom/opt/zoom/Qt/lib:$root/.runtime/zoom/opt/zoom:$LD_LIBRARY_PATH" \
    ldd "$root/.runtime/zoom/opt/zoom/zoom" | sed -n '/not found/p')
if [[ -n "$missing" ]]; then printf '%s\n' "$missing" >&2; exit 1; fi
command -v tesseract >/dev/null || { echo 'Install tesseract-ocr before enabling desktop mode.' >&2; exit 1; }
printf 'Zoom Desktop %s ready (rootless).\n' "$version"

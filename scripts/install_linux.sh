#!/usr/bin/env bash
# Installs pyWatts for the current user (Ubuntu 24.04 or newer). Never uses sudo.
#   scripts/install_linux.sh               install or update
#   scripts/install_linux.sh --uninstall   remove program and menu entry, keep all rides
set -euo pipefail

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
venv=$root/.venv
share=${XDG_DATA_HOME:-$HOME/.local/share}
entry=$share/applications/pywatts.desktop
icon=$share/icons/hicolor/scalable/apps/pywatts.svg

fail() {
    printf 'Error: %s\n' "$*" >&2
    exit 1
}

[ "$(uname -s)" = Linux ] || fail "this script is for Linux."
[ "$(id -u)" -ne 0 ] || fail "please run it as a normal user, without sudo."

# A .venv of Windows or of something else must not be touched.
ours() { [ -f "$venv/pyvenv.cfg" ] && [ -x "$venv/bin/python" ]; }

case "${1:-}" in
--uninstall)
    rm -f -- "$entry" "$icon"
    if ours; then rm -rf -- "$venv"; fi
    echo "Removed. Rides and settings are still in $share/pywatts."
    exit 0
    ;;
"") ;;
*) fail "unknown option $1" ;;
esac

if [ -e "$venv" ] && ! ours; then
    fail "$venv belongs to another system. Use a separate copy of the project for Linux."
fi
case "$root" in
*[\"\\\`\$%]*) fail "the folder name contains a character that menu entries cannot have: $root" ;;
esac

python=${PYTHON:-python3}
command -v "$python" >/dev/null || fail "$python was not found."
"$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' ||
    fail "Python 3.11 or newer is needed (Ubuntu 24.04 has it), found $("$python" --version)."

missing=()
"$python" -c 'import ensurepip, venv' 2>/dev/null || missing+=(python3-venv)
if command -v dpkg-query >/dev/null; then
    for package in libxcb-cursor0 libegl1 libxkbcommon-x11-0 bluez; do
        [ "$(dpkg-query -W -f '${db:Status-Status}' "$package" 2>/dev/null)" = installed ] ||
            missing+=("$package")
    done
fi
if [ "${#missing[@]}" -gt 0 ]; then
    if [ "${PYWATTS_SKIP_CHECK:-}" = 1 ]; then
        echo "Warning, missing: ${missing[*]}" >&2
    else
        fail "packages are missing. Please run this first:
  sudo apt install ${missing[*]}"
    fi
fi

ours || "$python" -m venv "$venv"
"$venv/bin/python" -m pip install --quiet --upgrade pip
"$venv/bin/python" -m pip install --quiet -e "$root"

mkdir -p -- "$(dirname -- "$entry")" "$(dirname -- "$icon")"
cp -- "$root/src/pywatts/ui/assets/icon.svg" "$icon"
cat >"$entry" <<EOF
[Desktop Entry]
Type=Application
Name=pyWatts
Comment=Structured workouts on a smart trainer
Exec="$venv/bin/pywatts"
Icon=pywatts
Terminal=false
Categories=Sports;Utility;
StartupWMClass=pywatts
EOF
if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$(dirname -- "$entry")" 2>/dev/null || true
fi

echo "Installed. Start pyWatts from the menu, or with: $venv/bin/pywatts"
echo "Without a trainer: $venv/bin/pywatts --fake"

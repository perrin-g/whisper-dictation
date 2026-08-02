#!/usr/bin/env bash
# Install the Whisper dictation tool on a GNOME/Wayland machine.
#
# Replicates the setup built and tested on the source machine: offline
# faster-whisper transcription, en-NZ spelling pass, and keyboard injection via
# the xdg-desktop-portal RemoteDesktop interface held open by a resident systemd
# user service (one consent prompt per login, no /dev/uinput, no root daemon).
#
# Idempotent-ish: safe to re-run. Does NOT use sudo except for the one package
# install step, which it prints and asks you to confirm.
set -euo pipefail

SHARE="$HOME/.local/share/whisper-dictation"
BIN="$HOME/.local/bin"
UNIT="$HOME/.config/systemd/user/whisper-typist.service"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/whisper-dictation"
MODEL_REPO="https://huggingface.co/Systran/faster-whisper-small.en"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }
die() { printf '\033[31merror: %s\033[0m\n' "$1" >&2; exit 1; }

# --- 0. preflight: confirm GNOME/Wayland + portal -------------------------
say "Preflight"
[ "${XDG_SESSION_TYPE:-}" = "wayland" ] || \
  echo "  warning: XDG_SESSION_TYPE is '${XDG_SESSION_TYPE:-unset}', expected 'wayland'"
command -v gdbus >/dev/null || echo "  warning: gdbus not found (needed to verify portal)"
echo "  desktop: ${XDG_CURRENT_DESKTOP:-unknown}  session: ${XDG_SESSION_TYPE:-unknown}"

# --- 1. system packages ---------------------------------------------------
say "System packages"
# We need: PyGObject + GLib introspection (portal D-Bus client), PipeWire's
# pw-record (mic capture), libnotify's notify-send (toasts), python3 venv.
if command -v apt-get >/dev/null; then
  PKGS="python3-gi gir1.2-glib-2.0 python3-venv pipewire-bin libnotify-bin"
  INSTALL="sudo apt-get install -y $PKGS"
elif command -v dnf >/dev/null; then
  PKGS="python3-gobject glib2 pipewire-utils libnotify"
  INSTALL="sudo dnf install -y $PKGS"
elif command -v pacman >/dev/null; then
  PKGS="python-gobject glib2 pipewire libnotify"
  INSTALL="sudo pacman -S --needed $PKGS"
else
  die "no known package manager (apt/dnf/pacman). Install PyGObject, GLib introspection, pipewire (pw-record), libnotify manually, then re-run with WD_SKIP_PKGS=1."
fi
if [ "${WD_SKIP_PKGS:-0}" = "1" ]; then
  echo "  WD_SKIP_PKGS=1 -> skipping package install"
else
  echo "  about to run: $INSTALL"
  read -r -p "  proceed? [y/N] " ans
  [ "$ans" = "y" ] || die "aborted at package install (set WD_SKIP_PKGS=1 to skip)"
  $INSTALL
fi

# --- 2. copy program files ------------------------------------------------
say "Install files -> $SHARE"
mkdir -p "$SHARE" "$BIN" "$(dirname "$UNIT")"
cp "$SRC"/transcribe.py "$SRC"/portal_type.py "$SRC"/portal_typed.py \
   "$SRC"/en_nz.py "$SRC"/requirements.txt "$SRC"/model.sha256 "$SHARE/"
cp "$SRC"/test_*.py "$SHARE/"
cp "$SRC"/dictate-toggle "$BIN/dictate-toggle"
chmod +x "$BIN/dictate-toggle"
cp "$SRC"/whisper-typist.service "$UNIT"

# --- 3. venv with system gi visible --------------------------------------
say "Python venv"
# CRITICAL: PyGObject ('gi') is installed by the system package manager and is
# only visible to the SYSTEM python3. Build the venv from /usr/bin/python3 with
# --system-site-packages, or 'import gi' will fail. (A pyenv/mise python will
# NOT see the apt/dnf gi.)
SYS_PY="$(command -v python3)"
case "$SYS_PY" in
  /usr/bin/*) ;;
  *) echo "  warning: python3 resolves to '$SYS_PY' (not /usr/bin). If 'import gi'"
     echo "           fails below, re-run with WD_PY=/usr/bin/python3" ;;
esac
PY_FOR_VENV="${WD_PY:-$SYS_PY}"
"$PY_FOR_VENV" -m venv --system-site-packages "$SHARE/venv"
"$SHARE/venv/bin/python" - <<'EOF' || die "PyGObject (gi) not visible in venv - install the system GObject package and re-run with WD_PY=/usr/bin/python3"
import gi  # noqa
gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa
print("  gi OK")
EOF

# --- 4. pinned, hash-verified dependencies --------------------------------
say "Python dependencies (pinned + hash-verified)"
# If a hash mismatches (different wheel for the target's Python/arch), pip will
# refuse. In that case regenerate hashes on the target with pip-compile or pip
# download, but keep them pinned.
"$SHARE/venv/bin/pip" install --upgrade pip >/dev/null
"$SHARE/venv/bin/pip" install --require-hashes -r "$SHARE/requirements.txt" || \
  die "pinned install failed - likely a wheel-hash mismatch for this Python/arch. Regenerate requirements.txt hashes on the target (see README), keep them pinned, and re-run."

# --- 5. model: download + checksum verify ---------------------------------
say "Model (small.en) -> $SHARE/model"
# Checksums in model.sha256 use bare filenames, so verify from inside model/.
if [ -d "$SHARE/model" ] && (cd "$SHARE/model" && sha256sum -c "$SHARE/model.sha256" >/dev/null 2>&1); then
  echo "  model present and checksums match - skipping download"
else
  command -v git >/dev/null || die "git needed to fetch the model"
  command -v git-lfs >/dev/null || echo "  note: git-lfs recommended for large model files"
  [ -d "$SHARE/model" ] && mv "$SHARE/model" "$SHARE/model.old.$$"
  GIT_LFS_SKIP_SMUDGE=0 git clone --depth 1 "$MODEL_REPO" "$SHARE/model"
  echo "  verifying checksums..."
  (cd "$SHARE/model" && sha256sum -c "$SHARE/model.sha256") || \
    die "model checksum mismatch - the upstream model may have changed. Inspect before trusting; do not bypass. Old model (if any) preserved at $SHARE/model.old.$$"
  [ -d "$SHARE/model.old.$$" ] && echo "  (old model preserved at $SHARE/model.old.$$ - remove when satisfied)"
fi

# --- 6. run the test suite ------------------------------------------------
say "Test suite"
fail=0
for t in "$SHARE"/test_*.py; do
  if "$SHARE/venv/bin/python" "$t" >/tmp/wd_test.out 2>&1; then
    echo "  PASS $(basename "$t")"
  else
    echo "  FAIL $(basename "$t") - see /tmp/wd_test.out"; fail=1
  fi
done
[ "$fail" = "0" ] || die "tests failed - stopping before enabling the service"

# --- 7. systemd user service ----------------------------------------------
say "Resident typist service"
systemctl --user daemon-reload
systemctl --user enable --now whisper-typist.service
echo "  NOTE: on first start GNOME shows ONE consent prompt - approve it."
echo "  The socket appears only AFTER you approve (that gate is the security control)."

# --- 8. GNOME hotkey ------------------------------------------------------
say "Hotkey (Super+\\)"
SCHEMA="org.gnome.settings-daemon.plugins.media-keys"
KEY="custom-keybindings"
# Find a free custom slot rather than clobbering existing ones.
existing="$(gsettings get $SCHEMA $KEY)"
slot=0
while echo "$existing" | grep -q "custom$slot/"; do slot=$((slot+1)); done
path="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom$slot/"
if [ "$existing" = "@as []" ] || [ "$existing" = "[]" ]; then
  newlist="['$path']"
else
  newlist="$(echo "$existing" | sed "s|]$|, '$path']|")"
fi
gsettings set $SCHEMA $KEY "$newlist"
sub="$SCHEMA.custom-keybinding:$path"
gsettings set "$sub" name "Whisper Dictation"
gsettings set "$sub" command "$BIN/dictate-toggle"
gsettings set "$sub" binding "<Super>backslash"
echo "  bound Super+\\ -> dictate-toggle (slot custom$slot)"

say "Done"
cat <<EOF
  Press Super+\\ to start dictation, speak, press again to stop and type.
  First use this login: approve the one GNOME consent prompt.

  Controls:
    systemctl --user status  whisper-typist     # check daemon
    systemctl --user restart whisper-typist     # re-grant (costs one prompt)
    systemctl --user stop    whisper-typist     # stop (removes socket)
    pkill -f portal_typed.py                     # hard kill
EOF

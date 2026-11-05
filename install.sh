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
# Pinned to an exact upstream commit (the repo's last, 2023-11-23) so a re-upload
# can't change what we fetch. Trust still comes from model.sha256, checked below;
# bump MODEL_REV and model.sha256 together. WD_MODEL_REV overrides it (the
# checksums are still enforced).
MODEL_REV="${WD_MODEL_REV:-d1d751a5f8271d482d14ca55d9e2deeebbae577f}"
MODEL_URL="https://huggingface.co/Systran/faster-whisper-small.en/resolve/$MODEL_REV"

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
# pw-record (mic capture) and pw-dump + jq (mic ranking in pick-source),
# libnotify's notify-send (toasts), python3 venv.
if command -v apt-get >/dev/null; then
  PKGS="python3-gi gir1.2-glib-2.0 python3-venv pipewire-bin libnotify-bin jq socat wl-clipboard git"
  INSTALL="sudo apt-get install -y $PKGS"
elif command -v dnf >/dev/null; then
  PKGS="python3-gobject glib2 pipewire-utils libnotify jq socat wl-clipboard git"
  INSTALL="sudo dnf install -y $PKGS"
elif command -v pacman >/dev/null; then
  PKGS="python-gobject glib2 pipewire libnotify jq socat wl-clipboard git"
  INSTALL="sudo pacman -S --needed $PKGS"
else
  die "no known package manager (apt/dnf/pacman). Install PyGObject, GLib introspection, pipewire (pw-record, pw-dump), jq, socat, wl-clipboard, git, libnotify manually, then re-run with WD_SKIP_PKGS=1."
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
# pick-source lives beside the program files, not in $BIN: dictate-toggle calls
# it as "$BASE/pick-source", and test_pick_source.py runs it from there.
cp "$SRC"/pick-source "$SHARE/pick-source"
chmod +x "$SHARE/pick-source"
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
# requirements.txt carries wheel hashes for CPython 3.12-3.15 only.
"$PY_FOR_VENV" -c 'import sys; sys.exit(not ((3, 12) <= sys.version_info[:2] <= (3, 15)))' || \
  die "$("$PY_FOR_VENV" -V 2>&1) is outside the supported 3.12-3.15 range (requirements.txt has no wheel hashes for it). See README 'Hash mismatch' to extend the lock."
"$PY_FOR_VENV" -m venv --system-site-packages "$SHARE/venv"
"$SHARE/venv/bin/python" - <<'EOF' || die "PyGObject (gi) not visible in venv - install the system GObject package and re-run with WD_PY=/usr/bin/python3"
import gi  # noqa
gi.require_version("Gio", "2.0")
from gi.repository import Gio  # noqa
print("  gi OK")
EOF

# --- 4. pinned, hash-verified dependencies --------------------------------
say "Python dependencies (pinned + hash-verified)"
# requirements.txt is a complete lock with x86_64 wheel hashes for CPython
# 3.12-3.15. --no-deps because it deliberately omits faster-whisper's
# onnxruntime dependency (only used by the VAD filter, which transcribe.py
# doesn't enable) and huggingface-hub's pyyaml (never imported here). If a hash
# mismatches (new Python/arch), add that wheel's hash - keep everything pinned.
"$SHARE/venv/bin/pip" install --upgrade pip >/dev/null
"$SHARE/venv/bin/pip" install --no-deps --require-hashes -r "$SHARE/requirements.txt" || \
  die "pinned install failed - likely a wheel-hash mismatch for this Python/arch. Regenerate requirements.txt hashes on the target (see README), keep them pinned, and re-run."

# --- 5. model: download + checksum verify ---------------------------------
say "Model (small.en) -> $SHARE/model"
# Checksums in model.sha256 use bare filenames, so verify from inside model/.
if [ -d "$SHARE/model" ] && (cd "$SHARE/model" && sha256sum -c "$SHARE/model.sha256" >/dev/null 2>&1); then
  echo "  model present and checksums match - skipping download"
else
  command -v curl >/dev/null || die "curl is needed to fetch the model"
  # Download each file into a staging dir (resumable: a half-finished *.part is
  # continued on re-run), verify the lot, and only then swap it into place.
  STAGE="$SHARE/model.partial"
  mkdir -p "$STAGE"
  while read -r want name; do
    [ -n "$name" ] || continue
    if [ -f "$STAGE/$name.part" ] && [ "$(sha256sum "$STAGE/$name.part" | cut -d' ' -f1)" = "$want" ]; then
      mv "$STAGE/$name.part" "$STAGE/$name"   # finished earlier, never renamed
    fi
    if [ -f "$STAGE/$name" ] && [ "$(sha256sum "$STAGE/$name" | cut -d' ' -f1)" = "$want" ]; then
      echo "  have $name"; continue
    fi
    echo "  fetching $name"
    rc=0
    curl -fL --retry 3 -C - -o "$STAGE/$name.part" "$MODEL_URL/$name" || rc=$?
    if [ "$rc" = "22" ]; then
      die "HTTP error fetching $MODEL_URL/$name - most likely a 404: the pinned model revision is gone. Fix: (1) 'git pull' for an installer with a newer pin; or (2) WD_MODEL_REV=<commit-or-main> bash install.sh (checksums are still enforced, so if the files differ you must update model.sha256 yourself); or (3) download the files listed in model.sha256 into $SHARE/model by hand and re-run."
    elif [ "$rc" != "0" ]; then
      die "download failed (curl exit $rc) - network problem. Re-run to resume."
    fi
    mv "$STAGE/$name.part" "$STAGE/$name"
  done < "$SHARE/model.sha256"
  echo "  verifying checksums..."
  (cd "$STAGE" && sha256sum -c "$SHARE/model.sha256") || {
    rm -rf "$STAGE"
    die "model checksum mismatch - the download is corrupt or the pinned revision changed. Inspect before trusting; do not bypass."
  }
  [ -d "$SHARE/model" ] && mv "$SHARE/model" "$SHARE/model.old.$$"
  mv "$STAGE" "$SHARE/model"
  [ -d "$SHARE/model.old.$$" ] && echo "  (old model preserved at $SHARE/model.old.$$ - remove when satisfied)"
fi

# --- 6. run the test suite ------------------------------------------------
say "Test suite"
fail=0
TESTLOG="$(mktemp "${TMPDIR:-/tmp}/wd_test.XXXXXX")"
for t in "$SHARE"/test_*.py; do
  if "$SHARE/venv/bin/python" "$t" >"$TESTLOG" 2>&1; then
    echo "  PASS $(basename "$t")"
  else
    echo "  FAIL $(basename "$t") - see $TESTLOG"; fail=1
    cat "$TESTLOG" >> "$TESTLOG.failed"
  fi
done
[ "$fail" = "0" ] || die "tests failed - stopping before enabling the service (log: $TESTLOG.failed)"
rm -f "$TESTLOG"

# --- 7. systemd user service ----------------------------------------------
say "Resident typist service"
systemctl --user daemon-reload
systemctl --user enable --now whisper-typist.service
echo "  NOTE: on first start GNOME shows ONE consent prompt - approve it."
echo "  The socket appears only AFTER you approve (that gate is the security control)."

# --- 8. GNOME hotkeys -----------------------------------------------------
say "Hotkeys (Super+\\, Super+Shift+\\)"
SCHEMA="org.gnome.settings-daemon.plugins.media-keys"
KEY="custom-keybindings"
PREFIX="/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings"

# bind_key <name> <command> <binding>
# Re-reads the slot list on every call, so binding several keys in one run does
# not put them all in the same slot. Reuses the slot already holding <name>, so
# re-running the installer updates a binding instead of adding a duplicate.
bind_key() {
  local name="$1" command="$2" binding="$3"
  local existing slot path sub reuse=""
  existing="$(gsettings get $SCHEMA $KEY)"

  # Already installed under this name? Update that slot in place.
  local i=0
  while [ "$i" -lt 30 ]; do
    if echo "$existing" | grep -q "custom$i/"; then
      if [ "$(gsettings get "$SCHEMA.custom-keybinding:$PREFIX/custom$i/" name 2>/dev/null)" = "'$name'" ]; then
        reuse="$i"
        break
      fi
    fi
    i=$((i+1))
  done

  if [ -n "$reuse" ]; then
    slot="$reuse"
    path="$PREFIX/custom$slot/"
  else
    slot=0
    while echo "$existing" | grep -q "custom$slot/"; do slot=$((slot+1)); done
    path="$PREFIX/custom$slot/"
    local newlist
    if [ "$existing" = "@as []" ] || [ "$existing" = "[]" ]; then
      newlist="['$path']"
    else
      newlist="$(echo "$existing" | sed "s|]$|, '$path']|")"
    fi
    gsettings set $SCHEMA $KEY "$newlist"
  fi

  sub="$SCHEMA.custom-keybinding:$path"
  gsettings set "$sub" name "$name"
  gsettings set "$sub" command "$command"
  gsettings set "$sub" binding "$binding"
  echo "  bound $binding -> ${command##*/} (slot custom$slot)"
}

# One hotkey only. --retype injects, and injecting from a modified hotkey turns the
# transcript into Super+key presses (Super+t, Super+L) when the modifier is still
# held; every dictation copies to the clipboard instead, so Ctrl+V is the recovery.
bind_key "Whisper Dictation" "$BIN/dictate-toggle" "<Super>backslash"

say "Done"
cat <<EOF
  Press Super+\\ to start dictation, speak, press again to stop and type.
  First use this login: approve the one GNOME consent prompt.

  Every dictation also goes to the clipboard, so if the text landed in the
  wrong window, or the inject failed, just press Ctrl+V where you want it.

  Controls:
    systemctl --user status  whisper-typist     # check daemon
    systemctl --user restart whisper-typist     # re-grant (costs one prompt)
    systemctl --user stop    whisper-typist     # stop (removes socket)
    pkill -f portal_typed.py                     # hard kill
EOF

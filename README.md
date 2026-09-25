# Whisper Dictation

Offline voice dictation for GNOME/Wayland: press `Super+\`, speak, press again;
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcribes locally
(no cloud, no network calls), the text is rewritten to en-NZ spelling, and it's
typed into whatever window has focus.

Built and tested on Ubuntu, GNOME 50, Wayland.

## Why not uinput

Most Linux voice tools inject via `/dev/uinput`. That requires root or `input`
group membership (which grants read access to every keyboard and mouse on the
box) and bypasses Wayland's security model entirely — the compositor can't see
it, mediate it, or ask you about it.

This project uses **`org.freedesktop.portal.RemoteDesktop`** instead — the same
compositor-mediated channel GNOME uses for screen-sharing and remote-control:

- **No root, no `input` group, no `/dev/uinput`.** Runs as your user under a
  hardened systemd `--user` unit (`NoNewPrivileges`, `ProtectSystem=strict`).
- **Keyboard only.** `SelectDevices` is called with the `KEYBOARD` bit only —
  the session cannot move the mouse or click regardless of what the code does.
- **You approve it.** GNOME shows its own native consent dialog on first use
  each login. The daemon can't silently start typing.
- **One prompt per login.** GNOME's portal has no working restore token, so
  `portal_typed.py` is a resident daemon that holds a single session open for
  the life of the login — approve once, dictate as many times as you like.

## Requirements

- GNOME (Shell 50+) on **Wayland**
- PipeWire (`pw-record` to capture, `pw-dump` to enumerate mics) and `jq`
- System Python 3 with distro PyGObject
- `socat`
- `wpctl` (WirePlumber) for the mute guard - optional, a missing `wpctl` just
  skips the check
- ~500 MB disk for the `small.en` model

## Install

```bash
bash install.sh
```

## Usage

- **`Super+\`** - first press records, second press stops, transcribes and types.
- **`Super+Shift+\`** - retypes the last transcript into whatever has focus now.
  Safe mid-recording, and repeatable: only your next dictation replaces it.

`Super+\` works even with the daemon down (first use this login, or after a screen
lock): it notifies you and raises the consent prompt. Approve, then press again to
dictate.

## What's in here

```
install.sh                 # installer (idempotent-ish)
whisper-dictation/         # program files + full test suite
  transcribe.py            # faster-whisper small.en + en-NZ pass, checksum-gated
  en_nz.py                 # US -> en-NZ spelling (curated map, not blind regex)
  portal_type.py           # socket client -> daemon
  portal_typed.py          # resident typist daemon (holds the portal session)
  dictate-toggle           # flock toggle: record <-> stop+transcribe+type
  pick-source              # ranks mics: headset first, built-in mic as fallback
  whisper-typist.service   # systemd --user unit (hardened)
  requirements.txt         # pinned + hash-verified deps
  model.sha256             # expected model checksums (verified on install)
  test_*.py                # 68 tests, run by install.sh before enabling service
```

## Notes

- **venv must see system `gi`.** `install.sh` uses `--system-site-packages`. A
  pyenv/mise Python won't see the distro's PyGObject. Override with
  `WD_PY=/usr/bin/python3` if needed.
- **What costs a consent prompt.** A screen lock invalidates the portal session,
  so the daemon exits cleanly (it watches the session `Closed` signal) and unlinks
  its socket; `systemctl --user restart` does the same deliberately. Either way the
  next press re-prompts. Nothing else does.
- **Held modifiers are released before typing.** Mutter's virtual input device
  shares the real keymap state, so a modifier still down when injection starts is
  combined with every keysym: `t` arrives as `Super+t` and fires whatever owns
  that hotkey, space as `Super+space`, and a held Shift uppercases everything and
  turns `,` into `<`. Both injection paths release Shift/Ctrl/Alt/Super first,
  after a 120 ms settle (`WD_SETTLE_MS`) for the triggering key to lift.
- **Nothing detects a stolen focus**, because GNOME on Wayland exposes no
  supported way to read the focused window (`org.gnome.Shell.Eval` is locked, no
  `wlr-foreign-toplevel`). The portal types wherever focus is at inject time, so
  instead of guessing, the design makes a miss cheap to undo: every transcript is
  stashed at `$XDG_RUNTIME_DIR/dictate.last` (0600) *before* the inject, for
  `Super+Shift+\` to replay. A failed inject also copies to the clipboard and says
  so. On tmpfs the stash never reaches disk and dies with the login session - but
  it follows the script's `/tmp` fallback if `XDG_RUNTIME_DIR` is unset, which may
  be real disk. Notifications truncate past 40 chars, since the shell keeps
  notification history.
- **Mic follows your headset.** Every press re-picks: a connected headset
  (Bluetooth or USB) beats the built-in mic, and a webcam mic ranks *below* it.
  `pick-source` ranks by device class (`device.form-factor` / `device.bus`), not by
  a hardcoded device, so a new headset works the first time you pair it - though a
  headset in playback-only (A2DP) profile publishes no capture node and so can't be
  picked. The recorder is pinned with `--target`, so dictation can use the headset
  while other apps stay on your system default. A failed probe falls back to the
  default source rather than blocking dictation.
- **en-NZ spelling** is a curated word map — not blanket suffix rules (those
  mis-fire on size/prize/doctor/error). High-collision words (check/cheque,
  tire/tyre, curb/kerb, draft/draught) are left untouched. Extend in `en_nz.py`.
- **Hash mismatch on `pip install`** means the target's Python/arch wants a
  different wheel. Regenerate hashes on the target — keep them pinned, don't
  drop `--require-hashes`.

## Controls

```bash
systemctl --user status  whisper-typist     # is the daemon up?
systemctl --user restart whisper-typist     # re-grant (one prompt)
systemctl --user stop    whisper-typist     # stop (removes socket)
dictate-toggle --retype                     # what Super+Shift+\ runs
pick-source                                 # which mic would be used right now
WD_SOURCE=<node.name> dictate-toggle        # prefer one mic (falls back if absent)
```

---

## Driver prompt for the target machine's coding assistant

> Paste the following to your AI coding assistant (Claude Code, Cursor, Codex
> CLI, Gemini CLI, etc.) on the new machine, with this bundle present.

You're setting up a replicated voice-dictation tool on this machine. The bundle
is in this directory. Do this:

1. Read `README.md` and skim `install.sh` and `whisper-dictation/*.py` so you
   understand what you're installing before running anything.
2. Confirm the target is GNOME on Wayland (`echo $XDG_SESSION_TYPE`,
   `$XDG_CURRENT_DESKTOP`). If it isn't, STOP and tell me — the injection layer
   won't work and we'll need a different approach.
3. Run `bash install.sh`. It will pause before the one `sudo` package step —
   let me approve it. If `import gi` fails in the venv check, re-run with
   `WD_PY=/usr/bin/python3`. If the pinned `pip install` fails on a hash
   mismatch, regenerate the hashes on this machine but keep them pinned and
   hash-verified — do not drop `--require-hashes`.
4. After install, confirm all tests passed (install.sh stops if not) and the
   `whisper-typist` service is active. Tell me to approve the first GNOME
   consent prompt, then verify the socket appears at
   `$XDG_RUNTIME_DIR/whisper-dictation.sock` (mode 0600).
5. Security re-check, and report results: I am NOT in the `input` group,
   `/dev/uinput` is still root-only, there's no ydotoold running, and no
   restore-token file exists on disk.
6. End-to-end test: tell me to focus a text editor and press `Super+\`. If the
   daemon is not yet running you'll see a notification and the consent prompt -
   approve it, then press `Super+\` again to start recording. Press once more to
   stop. Confirm the text appears with en-NZ spelling and that only ONE consent
   prompt fired for the whole session.

Work security-first, build a task list with validation gates, and test as you
go. Don't assume a platform capability works — probe it. Stop and ask if
anything diverges from the README's stated assumptions.

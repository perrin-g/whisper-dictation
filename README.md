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

`Super+\` works at any time:

- **Daemon running:** first press starts recording, second press stops,
  transcribes, and types.
- **Daemon not running** (first use this login, or after a screen lock): a
  notification appears followed immediately by the GNOME consent prompt.
  Approve it, then press `Super+\` again to start dictating. A "Ready" notice
  confirms the daemon is up.

The daemon holds the grant open — no further prompts until the service restarts
or you log out.

`Super+Shift+\` types the **last transcript** into whatever has focus now. That's
the recovery path for the two ways dictated text gets lost: focus moved to
another window while you were speaking, so the words went somewhere else, or the
inject failed outright. Safe to press mid-recording, and repeatable - the stash
is only replaced by your next dictation.

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
  test_*.py                # 66 tests, run by install.sh before enabling service
```

## Notes

- **venv must see system `gi`.** `install.sh` uses `--system-site-packages`. A
  pyenv/mise Python won't see the distro's PyGObject. Override with
  `WD_PY=/usr/bin/python3` if needed.
- **Screen lock.** The daemon subscribes to the portal session `Closed` signal.
  When the lock screen invalidates the session the daemon exits cleanly and
  unlinks its socket. The next `Super+\` after unlock sees the daemon as down,
  fires the consent prompt immediately, and you're dictating within a few
  seconds of approving it.
- **Restart costs a prompt.** `systemctl --user restart whisper-typist` tears
  down the held session, so the next start re-prompts.
- **Dictated text is hard to lose.** Each transcript is stashed at
  `$XDG_RUNTIME_DIR/dictate.last` (mode 0600) *before* the inject is attempted, so
  a failure or a stolen focus is recoverable with `Super+Shift+\`. On tmpfs it
  never reaches disk and dies with the login session. If the inject fails the text
  also goes to the clipboard, and the notification says so. Note the stash follows
  the script's `/tmp` fallback if `XDG_RUNTIME_DIR` is unset, which may be real
  disk. Notifications truncate transcripts past 40 characters, since the shell
  keeps notification history.
- **No focus detection.** GNOME on Wayland exposes no supported way to read the
  focused window (`org.gnome.Shell.Eval` is locked, and there's no
  `wlr-foreign-toplevel`), so the portal types into whatever has focus at inject
  time. Rather than guess, the design makes the miss cheap to undo.
- **Mic follows your headset.** Every press re-picks the mic: a connected headset
  (Bluetooth or USB) wins, otherwise the built-in mic. `pick-source` ranks by
  device class (`device.form-factor` / `device.bus` from `pw-dump`) rather than by
  a hardcoded device, so a new headset works the first time you pair it. A headset
  in playback-only (A2DP) profile publishes no capture node and so cannot be
  picked - the mic has to be live, not just paired. A webcam mic ranks *below* the
  built-in one. The recorder is pinned with `--target`, so this is independent of
  the system default input: dictation can use the headset while Teams and Chrome
  stay on whatever you set. The "recording via ..." notification names the mic in
  use, and the mute guard checks *that* mic. Run `pick-source` to see what would
  be used right now, or set `WD_SOURCE=<node.name>` to prefer one device (it still
  falls back when that device is absent). A failed probe degrades to the default
  source rather than blocking dictation.
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

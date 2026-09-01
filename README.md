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
- PipeWire (`pw-record`)
- System Python 3 with distro PyGObject
- `socat`
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

## What's in here

```
install.sh                 # installer (idempotent-ish)
whisper-dictation/         # program files + full test suite
  transcribe.py            # faster-whisper small.en + en-NZ pass, checksum-gated
  en_nz.py                 # US -> en-NZ spelling (curated map, not blind regex)
  portal_type.py           # socket client -> daemon
  portal_typed.py          # resident typist daemon (holds the portal session)
  dictate-toggle           # flock toggle: record <-> stop+transcribe+type
  whisper-typist.service   # systemd --user unit (hardened)
  requirements.txt         # pinned + hash-verified deps
  model.sha256             # expected model checksums (verified on install)
  test_*.py                # 40+ tests, run by install.sh before enabling service
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

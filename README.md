# Whisper Dictation

Offline voice dictation for GNOME/Wayland: press `Super+\`, speak, press again;
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcribes locally
(no cloud, no network calls), the text is rewritten to en-NZ spelling, and it's
typed into whatever window has focus.

Built and tested on Ubuntu, GNOME 50, Wayland.

## Why this isn't built the "standard" way

Most Linux voice-typing tools (dictation scripts, `nerd-dictation`, various
`ydotool`/`xdotool`-based tools) inject keystrokes through `/dev/uinput` or a
uinput-backed daemon like `ydotoold`. That's the standard pattern because it's
simple: open the device, emit key events, done. It has two costs that this
project deliberately avoids:

1. **Root or `input` group membership.** `/dev/uinput` is a raw kernel input
   device. Writing to it means either running as root or adding your user to
   the `input` group — which also grants read access to *every* input device
   on the box, including all physical keyboards and mice. A bug or compromise
   in the dictation tool becomes a full keylogger/input-injector with system
   scope, no window-manager awareness, and no audit trail.
2. **No compositor consent.** uinput injection happens below the compositor —
   Wayland's security model can't see it, mediate it, or ask you about it. The
   tool can type into *any* window, at *any* time, without you ever being
   asked. On X11 this was normal because X11 had no such boundary anyway; on
   Wayland it's a hole punched straight through the isolation Wayland exists
   to provide.

This project uses the **`org.freedesktop.portal.RemoteDesktop`** interface via
`xdg-desktop-portal` instead — the same compositor-mediated channel GNOME uses
for screen-sharing and remote-control tools. Concretely:

- **No root, no `input` group, no `/dev/uinput` access at all.** The daemon
  runs as your normal user under a hardened systemd `--user` unit
  (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome=read-only`).
- **Scoped to keyboard only.** `SelectDevices` is called with the `KEYBOARD`
  bit set and nothing else — the session cannot move the mouse, click, or
  touch, even if the code were compromised or buggy.
- **You approve it.** The first time the daemon starts each login, GNOME shows
  its own native consent dialog for the RemoteDesktop session. You see it, you
  approve it (or don't). The daemon can't silently start typing.
- **The prompt is closed-book, so this repo adds context around it.** The
  portal's own consent dialog is rendered by the compositor and its wording is
  fixed — a portal client cannot customise or fake that text (that's the
  point: apps can't lie about what they're asking for). To keep the prompt
  from arriving out of nowhere, `portal_typed.py` fires a plain-language
  desktop notification (`notify-send`, critical urgency, persists until
  dismissed) just before the portal dialog appears, explaining in the app's
  own words what's about to be asked and why.

The tradeoff for going through the portal instead of uinput: GNOME's
RemoteDesktop implementation has **no working restore token** (verified live —
`Start` returns none even with `persist_mode=2`). A token would let the daemon
skip the prompt on future logins, but a cached, replayable token is also a
skeleton key sitting on disk. Rather than treat the missing token as a bug to
work around, this project treats **one prompt per login as the actual
security control** — so `portal_typed.py` is a *resident* daemon that holds a
single portal session open for the life of the login, meaning you approve
exactly once (at daemon start) rather than once per dictation.

## Target requirements

- GNOME (Shell 50+) on **Wayland** — the injection path depends on the
  `org.freedesktop.portal.RemoteDesktop` interface this provides.
- PipeWire (`pw-record`) for mic capture.
- System Python 3 with the distro's PyGObject package available.
- `socat` — used for daemon liveness probing in `dictate-toggle`.
- ~500 MB disk for the `small.en` model.

Different distro is fine — `install.sh` detects apt/dnf/pacman. The injection
layer is GNOME/Wayland-specific, not distro-specific.

## Install

```bash
bash install.sh
```

`Super+\` works at any time, including when the daemon isn't running yet - it
starts the service for you. On first `Super+\` **this login**, if the
`whisper-typist` daemon is not running you'll get a notification saying so,
followed immediately by the GNOME consent prompt to start the service.
Approve it, then press `Super+\` again to dictate. The daemon then holds that
grant open so no further prompts until the service restarts or you log out.

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

## Notes / gotchas carried over from the build

- **venv must see system `gi`.** `install.sh` builds the venv from
  `/usr/bin/python3` with `--system-site-packages`. A pyenv/mise Python will NOT
  see the distro's PyGObject and `import gi` fails. Override with
  `WD_PY=/usr/bin/python3` if your `python3` resolves elsewhere.
- **No restore token on GNOME.** See above — this is why there's a resident
  daemon holding one session instead of a cached token.
- **Restart costs a prompt.** `systemctl --user restart whisper-typist` tears
  down the held session, so the next start re-prompts. Single-prompt-per-login
  holds only while the service starts once at login and stays up.
- **Daemon not running.** `Super+\` can be pressed at any time - if the daemon
  is not running (e.g. you cancelled the consent prompt earlier, or this is the
  first use this login), you'll see a notification and the consent prompt
  appears immediately, starting the service. Approve to start the service, then
  press `Super+\` again to dictate. Cancelling exits the daemon cleanly - it
  will not loop re-prompting.
- **Notifications are honest.** The daemon acks `OK`/`ERR` to the client after
  each inject. If its held portal session has died (e.g. it logs
  `Invalid state`), the toggle shows "type failed - restart..." instead of
  falsely claiming "typed: ...". The fix when you see that is a daemon restart
  (one prompt). The daemon does NOT fall back to a one-shot session on failure -
  that would silently re-prompt - it fails loudly instead.
- **Hash mismatch on `pip install`** means the target's Python/arch wants a
  different wheel. Regenerate hashes on the target (`pip download` then hash, or
  pip-compile) — keep them pinned, don't drop `--require-hashes`.
- **en-NZ spelling is a curated word map**, deliberately not blanket suffix
  rules (those mis-fire on size/prize/doctor/error). High-collision words
  (check/cheque, tire/tyre, curb/kerb, draft/draught) are left untouched to
  avoid changing meaning. Extend the map in `en_nz.py` as you hit gaps.

## Controls

```bash
systemctl --user status  whisper-typist     # is the daemon up?
systemctl --user restart whisper-typist     # re-grant (one prompt)
systemctl --user stop    whisper-typist     # stop (removes socket)
pkill -f portal_typed.py                     # hard kill if stuck
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

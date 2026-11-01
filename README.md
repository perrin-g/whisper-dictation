# Whisper Dictation

Offline voice dictation for GNOME/Wayland: press `Super+\`, speak, press again;
[faster-whisper](https://github.com/SYSTRAN/faster-whisper) transcribes locally
(no cloud, no network calls), the text is rewritten to en-NZ spelling, and it's
typed into whatever window has focus.

Built on Ubuntu (GNOME 50, Wayland). Clean-install tested on Fedora 44
(GNOME 50.6, Python 3.14) with `dnf`; the `apt` path is the original target and the
`pacman` package list is untested.

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
- `wl-clipboard` (`wl-copy`) for the clipboard copy - optional, the stash still works without it
- `wpctl` (WirePlumber) for the mute guard - optional, a missing `wpctl` just
  skips the check
- `git` and `curl` (to fetch this repo and the model)
- ~500 MB disk for the `small.en` model, ~120 MB for the Python packages
- Network access during install only; dictation itself makes no network calls

## Install

```bash
git clone https://github.com/perrin-g/whisper-dictation
cd whisper-dictation
bash install.sh
```

The installer asks once before its single `sudo` step (distro packages), then:
installs the Python packages (pinned and hash-verified), downloads the
`faster-whisper-small.en` model from a pinned Hugging Face revision and checks it
against `model.sha256`, runs the 70 tests, starts the `whisper-typist` service and
binds `Super+\`. It refuses to enable the service if anything fails. Re-running
is safe.

When it finishes, press `Super+\`, approve the one GNOME consent prompt, then
dictate: press again to start, once more to stop and type.

Overrides: `WD_SKIP_PKGS=1` skips the package step, `WD_PY=/usr/bin/python3` picks
the Python for the venv, `WD_MODEL_REV=<commit>` fetches a different model revision
(checksums are still enforced).

## Usage

- **`Super+\`** - first press records, second press stops, transcribes and types.
- **Every dictation also lands on the clipboard**, so if the text went to the wrong
  window or the inject failed, `Ctrl+V` puts it where you wanted it. No second
  hotkey - see the modifier note below for why that matters.

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
  test_*.py                # 70 tests, run by install.sh before enabling service
                           # (they run against the installed copy in
                           # ~/.local/share/whisper-dictation, not the checkout)
```

## Notes

- **venv must see system `gi`.** `install.sh` uses `--system-site-packages`. A
  pyenv/mise Python won't see the distro's PyGObject. Override with
  `WD_PY=/usr/bin/python3` if needed.
- **What costs a consent prompt.** A screen lock invalidates the portal session,
  so the daemon exits cleanly (it watches the session `Closed` signal) and unlinks
  its socket; `systemctl --user restart` does the same deliberately. Either way the
  next press re-prompts. Nothing else does.
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
- **Model download fails with an HTTP error (likely 404).** The pinned model
  revision has been removed upstream. Run `git pull` for a newer pin, or set
  `WD_MODEL_REV=<commit-or-main>` - if the files differ you must update
  `model.sha256` yourself, since the checksums are always enforced - or place the
  four files listed in `model.sha256` in `~/.local/share/whisper-dictation/model`
  and re-run. A network error just needs a re-run; the download resumes.
- **Hash mismatch on `pip install`** means the target's Python/arch wants a
  different wheel. Regenerate hashes on the target — keep them pinned, don't
  drop `--require-hashes`.

## Caveats

Read these before trusting it with anything sensitive.

- **Each transcript ends up in three places.** All are readable by anything running
  as your user; what differs is how routine that read is.

  | | Where | Read by | Lifetime | How much |
  |---|---|---|---|---|
  | Stash | `$XDG_RUNTIME_DIR/dictate.last`, tmpfs | only by opening that exact path | until the next dictation; gone at logout | full text |
  | Clipboard | `wl-copy` memory | focused Wayland apps, **plus any X11 client, unfocused, any time** | until your next copy | full text |
  | Notification | gnome-shell | on-screen; session-bus eavesdropping | until dismissed | first 40 chars |

  The clipboard is the widest: XWayland bridges it, so polling it is normal,
  unremarkable behaviour for any X11 app you happen to be running.

- **So don't dictate credentials.** Not because of a specific hole - because three
  copies exist and one of them is the clipboard.

- **A clipboard-history extension changes the picture.** Pano, Clipboard Indicator
  and friends log every copy *to disk*. Install one and every dictation becomes a
  permanent on-disk record. Nothing here persists to disk today.

- **`wl-copy --paste-once` is not recommended**, despite looking like the obvious
  hardening. Apps frequently request the selection more than once for type
  negotiation, which consumes it early and makes the paste appear broken. Set
  `WD_CLIP_CMD` if you want it anyway.

- **Nothing detects a stolen focus.** GNOME on Wayland exposes no supported way to
  read the focused window (`org.gnome.Shell.Eval` is locked, no
  `wlr-foreign-toplevel`), so the portal types wherever focus is at inject time. The
  design makes a miss cheap to undo rather than detectable: `Ctrl+V`, or
  `dictate-toggle --retype`.

- **Holding the trigger key can still corrupt a transcript.** Mutter's virtual input
  device shares the real keymap state, so a modifier physically down when injection
  starts combines with every keysym: `t` arrives as `Super+t` and fires whatever owns
  that hotkey, `l` as `Super+L` (screen lock), and a held Shift uppercases the text
  and turns `,` into `<`. Releasing the modifiers first does **not** fix it - a
  virtual device cannot clear a key held on the real keyboard, and there is no API
  to read the modifier state. The mitigation is a `WD_SETTLE_MS` (250 ms) wait plus
  the second of transcription, which covers a normal press but is **not a
  guarantee**. If you see it recur, move dictation to a key with no modifier
  (`Insert` is usually free) - that removes the cause rather than mitigating it.
  This is also why recovery is the clipboard and not a second hotkey, and why
  `--retype` is command-line only.

- **The stash follows a `/tmp` fallback** if `XDG_RUNTIME_DIR` is unset, and `/tmp`
  may be real disk. With it set (normal on a systemd login) the stash is tmpfs and
  never touches disk.

- **A Bluetooth headset mic may transcribe *worse* than your built-in one.** Headset
  capture runs over HFP/HSP, which is narrowband (CVSD at 8 kHz, mSBC at 16 kHz),
  while a decent laptop array mic is wideband and clean. `pick-source` prefers the
  headset because that is usually what you want when wearing one, but if accuracy
  drops when it connects, pin the built-in mic with
  `WD_SOURCE=alsa_input.<your-device>` - `pick-source` prints the node names.

- **`small.en` is a small model.** It is chosen for speed and offline use, not
  accuracy: it guesses badly at isolated letters, spelled-out words and unusual
  proper nouns. Dictate whole sentences and it does well; test it with the alphabet
  and it will disappoint you. Swap the model in `transcribe.py` (and `model.sha256`)
  if you want better at the cost of latency.

## Controls

```bash
systemctl --user status  whisper-typist     # is the daemon up?
systemctl --user restart whisper-typist     # re-grant (one prompt)
systemctl --user stop    whisper-typist     # stop (removes socket)
dictate-toggle --retype                     # type the last transcript again
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

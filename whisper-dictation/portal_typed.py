#!/usr/bin/env python3
"""Resident typist daemon.

Holds ONE xdg-desktop-portal RemoteDesktop session open for the whole login
session, so the GNOME consent prompt fires once at startup instead of on every
dictation. Listens on a unix socket ($XDG_RUNTIME_DIR/whisper-dictation.sock,
mode 0600); each client connection sends UTF-8 text, which the daemon types into
the focused window through the held session.

Subscribes to the portal session Closed signal (via a GLib mainloop thread) so
the daemon exits cleanly when the screen locks, unlinking the socket. This means
dictate-toggle's daemon_alive check returns false immediately after unlock, and
the first Super+backslash press triggers re-consent rather than a wasted recording cycle.

The socket-serving layer (serve / handle_connection) is decoupled from D-Bus: it
takes an `inject` callable, so the protocol is unit-testable with a stub injector
and no portal. The keysym mapping is reused from portal_type.text_to_keysyms.
"""
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from portal_type import DEVICE_KEYBOARD, MODIFIER_KEYSYMS, text_to_keysyms

# Portal D-Bus addressing (same target as the one-shot client).
PORTAL = "org.freedesktop.portal.Desktop"

_MAX_RECV = 65536  # 64 KB; no utterance is larger


class ConsentDenied(Exception):
    """User cancelled or denied the portal consent prompt."""
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.code = code  # portal response code: 1 = user cancelled, 2 = portal error


PATH = "/org/freedesktop/portal/desktop"
RD = "org.freedesktop.portal.RemoteDesktop"


def default_socket_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime, "whisper-dictation.sock")


# --- socket server (no D-Bus; takes an inject callable) ---------------------

def _reply(conn, msg):
    """Best-effort ack back to the client; the client may have already gone."""
    try:
        conn.sendall(msg)
    except OSError:
        pass


def handle_connection(conn, inject):
    """Read all UTF-8 bytes off one connection, inject, and ack OK/ERR.

    The client half-closes its write side after sending, so recv() returns
    empty once the text is in; we then inject and report the outcome back on
    the same connection. A failed injection still must not kill the daemon -
    we swallow it, log it, and tell the client ERR so it can warn the user
    instead of falsely claiming success.
    """
    conn.settimeout(10.0)
    chunks = []
    total = 0
    try:
        while True:
            b = conn.recv(65536)
            if not b:
                break
            total += len(b)
            if total > _MAX_RECV:
                sys.stderr.write("handle_connection: oversized payload, dropping\n")
                return
            chunks.append(b)
    except OSError:
        return
    text = b"".join(chunks).decode("utf-8", "replace")
    if not text:
        return
    try:
        inject(text)
    except ConsentDenied:
        # Portal session lost consent (e.g. after screen lock). Exit with failure
        # so systemd Restart=on-failure brings up a fresh session automatically.
        _reply(conn, b"ERR")
        raise
    except Exception as exc:  # one bad utterance must not kill the daemon
        sys.stderr.write(f"inject failed: {exc!r}\n")
        _reply(conn, b"ERR")
    else:
        _reply(conn, b"OK")


def serve(sock_path, inject, ready=None, should_stop=None):
    """Listen on sock_path; for each connection read text and call inject(text).

    ready: optional callable invoked once the socket is bound, chmodded and
    listening (lets tests synchronise). should_stop: optional callable polled
    between accepts so the loop can be torn down (used by tests); None = run
    forever.
    """
    _prepare_socket_path(sock_path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.settimeout(0.2)
    try:
        srv.bind(sock_path)
        os.chmod(sock_path, 0o600)
        srv.listen(8)
        if ready:
            ready()
        while not (should_stop and should_stop()):
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            with conn:
                handle_connection(conn, inject)
    finally:
        srv.close()
        _unlink_quietly(sock_path)


def _prepare_socket_path(sock_path):
    """Reclaim a stale socket file; refuse to start if a live daemon owns it."""
    if not os.path.exists(sock_path):
        return
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(sock_path)
    except OSError:
        probe.close()
        _unlink_quietly(sock_path)  # nobody listening -> stale, reclaim
        return
    probe.close()
    sys.exit(f"another whisper typist is already listening on {sock_path}")


def _unlink_quietly(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# --- portal session (D-Bus; held open across injections) --------------------

def _notify_consent_pending():
    """Best-effort desktop notification explaining the portal prompt that is
    about to appear. Returns the notification ID so the caller can dismiss it
    once the session is established."""
    try:
        result = subprocess.run(
            [
                "notify-send",
                "--app-name=Whisper Dictation",
                "--urgency=critical",  # stays on screen until dismissed, not missed if away
                "--expire-time=0",
                "--print-id",
                "Whisper Dictation needs one-time permission",
                "The next prompt lets it type transcribed speech into your "
                "focused window (keyboard input only - no mouse/touch, no "
                "screen access). Approve it once per login.",
            ],
            check=False,
            timeout=3,
            capture_output=True,
            text=True,
        )
        time.sleep(2.5)  # give the user time to read it before the portal dialog steals focus
        return result.stdout.strip() or None
    except OSError:
        return None  # notify-send missing; the portal prompt still works without it


def _dismiss_consent_notification(notif_id):
    """Replace the persistent consent notification with a short-lived ready message."""
    if not notif_id:
        return
    try:
        subprocess.run(
            [
                "notify-send",
                "--app-name=Whisper Dictation",
                f"--replace-id={notif_id}",
                "--expire-time=3000",
                "Whisper Dictation",
                "Ready - press the dictation key to start.",
            ],
            check=False,
            timeout=3,
        )
    except OSError:
        pass


def establish_session(state, notify=True):
    """Open a RemoteDesktop keyboard session and keep its bus connection in
    `state`. Blocks on the consent prompt; on success state has bus/handle set."""
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import GLib, Gio

    if notify:
        state["_consent_notif_id"] = _notify_consent_pending()

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    sender = bus.get_unique_name()[1:].replace(".", "_")
    loop = GLib.MainLoop()
    local = {"handle": None, "error": None}
    counter = {"n": 0}

    def new_token(prefix):
        counter["n"] += 1
        return f"{prefix}{counter['n']}"

    def call(method, params):
        return bus.call_sync(
            PORTAL, PATH, RD, method, params, None,
            Gio.DBusCallFlags.NONE, -1, None,
        )

    def on_response(conn, sndr, path, iface, signal, params, step):
        code, results = params.unpack()
        if code != 0:
            local["error"] = ConsentDenied(f"{step} cancelled/failed (code {code})", code=code)
            loop.quit()
            return
        try:
            steps[step](results)
        except Exception as exc:
            local["error"] = RuntimeError(f"{step} raised: {exc!r}")
            loop.quit()

    def subscribe(token, step):
        req_path = f"/org/freedesktop/portal/desktop/request/{sender}/{token}"
        return bus.signal_subscribe(
            PORTAL, "org.freedesktop.portal.Request", "Response", req_path,
            None, Gio.DBusSignalFlags.NONE, on_response, step,
        )

    def start_create(_):
        token = new_token("ct")
        subscribe(token, "created")
        opts = {
            "handle_token": GLib.Variant("s", token),
            "session_handle_token": GLib.Variant("s", new_token("sess")),
        }
        call("CreateSession", GLib.Variant("(a{sv})", (opts,)))

    def created(results):
        local["handle"] = results["session_handle"]
        token = new_token("sd")
        subscribe(token, "selected")
        opts = {
            "handle_token": GLib.Variant("s", token),
            "types": GLib.Variant("u", DEVICE_KEYBOARD),
        }
        call("SelectDevices",
             GLib.Variant("(oa{sv})", (local["handle"], opts)))

    def selected(_results):
        token = new_token("st")
        subscribe(token, "started")
        opts = {"handle_token": GLib.Variant("s", token)}
        call("Start",
             GLib.Variant("(osa{sv})", (local["handle"], "", opts)))

    def started(_results):
        loop.quit()

    steps = {"created": created, "selected": selected, "started": started}
    start_create(None)
    loop.run()
    if local["error"]:
        raise local["error"]
    state["bus"] = bus
    state["handle"] = local["handle"]
    state["GLib"] = GLib
    state["Gio"] = Gio


# A modifier the user is still holding when injection starts gets combined with
# every keysym sent: 't' arrives as Super+t (firing whatever owns that hotkey),
# space as Super+space, and a held Shift uppercases the lot and turns ',' into
# '<'. Releasing them first is what wtype and ydotool do. Shared with the one-shot
# client so both injection paths behave the same.
# Releasing cannot clear a modifier the user is PHYSICALLY holding - that state
# belongs to the real device and the compositor takes the union across devices.
# It only helps when a hotkey grab has latched one. The wait for real keys to lift
# is dictate-toggle's job, since only it knows which trigger fired.
_MODIFIER_KEYSYMS = MODIFIER_KEYSYMS


def _notify_keysym(state, keysym, pressed):
    bus = state["bus"]
    GLib = state["GLib"]
    Gio = state["Gio"]
    bus.call_sync(
        PORTAL, PATH, RD, "NotifyKeyboardKeysym",
        GLib.Variant("(oa{sv}iu)", (state["handle"], {}, keysym, 1 if pressed else 0)),
        None, Gio.DBusCallFlags.NONE, -1, None,
    )


def _emit(state, text):
    # The wait for a held trigger key to lift happens in dictate-toggle, which knows
    # which hotkey fired; by here the caller has already settled.
    # Release-only: we never press these. Releasing a modifier that was not down
    # is a no-op for the compositor; releasing one that IS down means it sees a
    # second release when the user lifts the physical key, which is harmless.
    for ks in _MODIFIER_KEYSYMS:
        _notify_keysym(state, ks, False)
    for ks in text_to_keysyms(text):
        _notify_keysym(state, ks, True)
        _notify_keysym(state, ks, False)


def make_injector(state):
    """Return inject(text) that types via the held session, re-establishing it
    once if the session has gone away."""
    def inject(text):
        if not state.get("bus"):
            establish_session(state)
        try:
            _emit(state, text)
        except Exception:
            try:
                establish_session(state, notify=False)  # session died; re-grant + retry silently
                _emit(state, text)
            except Exception as exc:
                # Retry also failed - session is unrecoverable. Exit so systemd
                # restarts with a fresh portal session after RestartSec.
                raise ConsentDenied(f"session unrecoverable after retry: {exc}") from exc
    return inject


def main():
    state = {}
    inject = make_injector(state)
    try:
        establish_session(state)  # take the one consent prompt up front, at startup
    except ConsentDenied as exc:
        sys.stderr.write(f"consent not granted: {exc}\n")
        # code 1 = user explicitly cancelled: exit 0 so systemd does not
        # restart us in a loop re-prompting indefinitely.
        # Any other code (e.g. 2 = portal in bad state after screen lock)
        # exits 1 so systemd retries after RestartSec, by which time the
        # portal should have recovered.
        sys.exit(0 if exc.code == 1 else 1)

    # Replace the persistent consent notification now that the session is ready.
    _dismiss_consent_notification(state.get("_consent_notif_id"))

    stopping = {"flag": False}
    GLib = state["GLib"]
    Gio = state["Gio"]
    glib_loop = GLib.MainLoop()

    def on_session_closed(*_):
        # Lock screen (or compositor restart) invalidated our portal session.
        # Exit cleanly so the socket is unlinked; dictate-toggle will start a
        # fresh session the next time the user presses Super+\.
        sys.stderr.write("portal session closed; exiting for re-consent on next use\n")
        stopping["flag"] = True
        glib_loop.quit()

    state["bus"].signal_subscribe(
        None, "org.freedesktop.portal.Session", "Closed",
        state["handle"], None, Gio.DBusSignalFlags.NONE,
        on_session_closed, None,
    )

    glib_thread = threading.Thread(target=glib_loop.run, daemon=True)
    glib_thread.start()

    def on_term(_signo, _frame):
        stopping["flag"] = True
        glib_loop.quit()

    # On SIGTERM/SIGINT, break out of the accept loop so serve()'s finally
    # block unlinks the socket - leaving a clean state for the next start.
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    try:
        serve(default_socket_path(), inject, should_stop=lambda: stopping["flag"])
    except ConsentDenied as exc:
        # Mid-session consent lost (e.g. screen lock invalidated the portal
        # session). Exit with failure so systemd restarts with a fresh session.
        sys.stderr.write(f"session lost consent: {exc}\n")
        sys.exit(1)
    finally:
        glib_loop.quit()
        glib_thread.join(timeout=2)


if __name__ == "__main__":
    main()

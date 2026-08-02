#!/usr/bin/env python3
"""Resident typist daemon.

Holds ONE xdg-desktop-portal RemoteDesktop session open for the whole login
session, so the GNOME consent prompt fires once at startup instead of on every
dictation. Listens on a unix socket ($XDG_RUNTIME_DIR/whisper-dictation.sock,
mode 0600); each client connection sends UTF-8 text, which the daemon types into
the focused window through the held session. If the portal session has died
(e.g. compositor restart), injection transparently re-establishes it - which
re-prompts - and retries once.

The socket-serving layer (serve / handle_connection) is decoupled from D-Bus: it
takes an `inject` callable, so the protocol is unit-testable with a stub injector
and no portal. The keysym mapping is reused from portal_type.text_to_keysyms.
"""
import os
import signal
import socket
import subprocess
import sys
import time

from portal_type import DEVICE_KEYBOARD, text_to_keysyms

# Portal D-Bus addressing (same target as the one-shot client).
PORTAL = "org.freedesktop.portal.Desktop"

_MAX_RECV = 65536  # 64 KB; no utterance is larger


class ConsentDenied(Exception):
    """User cancelled or denied the portal consent prompt."""


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
    about to appear. The portal's own dialog text is fixed by the compositor
    and cannot be customised, so this fires just before it to give context."""
    try:
        subprocess.run(
            [
                "notify-send",
                "--app-name=Whisper Dictation",
                "--urgency=critical",  # stays on screen until dismissed, not missed if away
                "--expire-time=0",
                "Whisper Dictation needs one-time permission",
                "The next prompt lets it type transcribed speech into your "
                "focused window (keyboard input only - no mouse/touch, no "
                "screen access). Approve it once per login.",
            ],
            check=False,
            timeout=3,
        )
    except OSError:
        pass  # notify-send missing; the portal prompt still works without it
    time.sleep(2.5)  # give the user time to read it before the portal dialog steals focus


def establish_session(state):
    """Open a RemoteDesktop keyboard session and keep its bus connection in
    `state`. Blocks on the consent prompt; on success state has bus/handle set."""
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import GLib, Gio

    _notify_consent_pending()

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
            local["error"] = ConsentDenied(f"{step} cancelled/failed (code {code})")
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


def _emit(state, text):
    bus = state["bus"]
    GLib = state["GLib"]
    Gio = state["Gio"]
    sh = state["handle"]
    for ks in text_to_keysyms(text):
        for st in (1, 0):  # press, release
            bus.call_sync(
                PORTAL, PATH, RD, "NotifyKeyboardKeysym",
                GLib.Variant("(oa{sv}iu)", (sh, {}, ks, st)),
                None, Gio.DBusCallFlags.NONE, -1, None,
            )


def make_injector(state):
    """Return inject(text) that types via the held session, re-establishing it
    once if the session has gone away."""
    def inject(text):
        if not state.get("bus"):
            establish_session(state)
        try:
            _emit(state, text)
        except Exception:
            establish_session(state)  # session likely died; re-grant + retry
            _emit(state, text)
    return inject


def main():
    state = {}
    inject = make_injector(state)
    try:
        establish_session(state)  # take the one consent prompt up front, at startup
    except ConsentDenied as exc:
        # User cancelled or denied - exit cleanly so Restart=on-failure does not
        # restart us in a loop re-prompting indefinitely.
        sys.stderr.write(f"consent not granted: {exc}\n")
        sys.exit(0)

    stopping = {"flag": False}

    def on_term(_signo, _frame):
        stopping["flag"] = True

    # On SIGTERM/SIGINT, break out of the accept loop so serve()'s finally
    # block unlinks the socket - leaving a clean state for the next start.
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    serve(default_socket_path(), inject, should_stop=lambda: stopping["flag"])


if __name__ == "__main__":
    main()

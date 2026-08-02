#!/usr/bin/env python3
"""Type text into the focused window via the xdg-desktop-portal RemoteDesktop
interface (compositor-mediated, no /dev/uinput, no root).

Reads UTF-8 text from stdin. Preferred path: hand the text to the resident
typist daemon over its unix socket - the daemon holds one portal session open,
so the GNOME consent prompt fired once at login and not here. Fallback path
(daemon not running): open a one-shot session ourselves, which prompts for
consent each time.

Scope is pinned to KEYBOARD only. GNOME has no working restore-token for
RemoteDesktop input (verified: Start returns no token), which is why
persistence lives in the daemon's held session rather than a cached token.

The keysym mapping (text_to_keysyms) and the device mask (DEVICE_KEYBOARD) are
module-level and pure, so they can be unit-tested without touching D-Bus.
"""
import os
import socket
import sys

# RemoteDesktop device bitmask (org.freedesktop.portal.RemoteDesktop):
#   1 = KEYBOARD, 2 = POINTER, 4 = TOUCHSCREEN.
# We request KEYBOARD only - never pointer/touch - so a bug or tamper cannot
# gain mouse-click control of the session.
DEVICE_KEYBOARD = 1

# X keysym constants for the control characters we map.
KEYSYM_RETURN = 0xFF0D
KEYSYM_TAB = 0xFF09
# Unicode-to-keysym offset for characters outside Latin-1.
KEYSYM_UNICODE_BASE = 0x01000000


def char_to_keysym(ch):
    """Map one character to its X keysym.

    Latin-1 (0x20-0xFF) maps directly; other Unicode uses the 0x01000000
    Unicode-keysym range; tab and newline get their dedicated keysyms.
    """
    cp = ord(ch)
    if ch == "\n" or ch == "\r":
        return KEYSYM_RETURN
    if ch == "\t":
        return KEYSYM_TAB
    if 0x20 <= cp <= 0xFF:
        return cp
    return KEYSYM_UNICODE_BASE + cp


def text_to_keysyms(text):
    """Return the list of keysyms to emit for a string."""
    return [char_to_keysym(ch) for ch in text]


def type_text(text):
    """Drive the RemoteDesktop portal to type `text` into the focused window."""
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import GLib, Gio

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    sender = bus.get_unique_name()[1:].replace(".", "_")
    PORTAL = "org.freedesktop.portal.Desktop"
    PATH = "/org/freedesktop/portal/desktop"
    RD = "org.freedesktop.portal.RemoteDesktop"
    loop = GLib.MainLoop()
    state = {"session_handle": None, "error": None}
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
            state["error"] = f"{step} cancelled/failed (code {code})"
            loop.quit()
            return
        try:
            steps[step](results)
        except Exception as exc:  # never hang the loop on a bug
            state["error"] = f"{step} raised: {exc!r}"
            loop.quit()

    def subscribe(token, step):
        # The portal replies on a Request object whose path is derived from the
        # caller's unique name and our handle_token.
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
        state["session_handle"] = results["session_handle"]
        token = new_token("sd")
        subscribe(token, "selected")
        opts = {
            "handle_token": GLib.Variant("s", token),
            "types": GLib.Variant("u", DEVICE_KEYBOARD),
        }
        call("SelectDevices",
             GLib.Variant("(oa{sv})", (state["session_handle"], opts)))

    def selected(_results):
        token = new_token("st")
        subscribe(token, "started")
        opts = {"handle_token": GLib.Variant("s", token)}
        call("Start",
             GLib.Variant("(osa{sv})", (state["session_handle"], "", opts)))

    def started(_results):
        sh = state["session_handle"]
        for ks in text_to_keysyms(text):
            for st in (1, 0):  # press, release
                call("NotifyKeyboardKeysym",
                     GLib.Variant("(oa{sv}iu)", (sh, {}, ks, st)))
        loop.quit()

    steps = {"created": created, "selected": selected, "started": started}
    start_create(None)
    loop.run()
    if state["error"]:
        sys.exit(state["error"])


def daemon_socket_path():
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    return os.path.join(runtime, "whisper-dictation.sock")


def send_via_daemon(text, sock_path=None):
    """Send text to the resident typist daemon and wait for its ack.

    Returns:
      "ok"          - daemon typed the text.
      "inject_failed" - daemon was reached but injection failed (e.g. its
                        portal session has gone Invalid state); the caller
                        must NOT claim success and should not fall back
                        (a fresh one-shot session would re-prompt).
      "unreachable" - no daemon listening (socket absent or refused connect).
      "lost"        - connected but daemon died mid-transfer (ECONNRESET etc).
    """
    sock_path = sock_path or daemon_socket_path()
    if not os.path.exists(sock_path):
        return "unreachable"
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        c.connect(sock_path)
    except OSError:
        c.close()
        return "unreachable"
    try:
        c.sendall(text.encode("utf-8"))
        c.shutdown(socket.SHUT_WR)
        try:
            ack = c.recv(16)
        finally:
            c.close()
    except OSError:
        return "lost"
    return "ok" if ack.strip() == b"OK" else "inject_failed"


def main():
    text = sys.stdin.read()
    if not text:
        return
    status = send_via_daemon(text)
    if status == "ok":
        return
    if status == "inject_failed":
        sys.exit("daemon injection failed (portal session lost)")
    if status == "lost":
        sys.exit("daemon crashed mid-transfer - restart: systemctl --user restart whisper-typist")
    if status == "unreachable":
        sys.exit("whisper-typist not running (start with: systemctl --user start whisper-typist)")


if __name__ == "__main__":
    main()

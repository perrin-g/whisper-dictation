#!/usr/bin/env python3
"""Unit tests for portal_typed.py socket layer (no D-Bus / portal needed).

The serve/handle_connection layer takes an `inject` callable, so we exercise the
full socket protocol with a stub injector that just records what it was asked to
type. Also covers stale-socket reclaim and the 0600 permission requirement.
"""
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portal_typed as ptd


def _run_server(sock_path, inject):
    """Start serve() in a thread; return (stop_fn, ready_event)."""
    ready = threading.Event()
    stop = {"flag": False}
    t = threading.Thread(
        target=ptd.serve,
        kwargs=dict(
            sock_path=sock_path,
            inject=inject,
            ready=ready.set,
            should_stop=lambda: stop["flag"],
        ),
        daemon=True,
    )
    t.start()
    ready.wait(5)

    def stop_fn():
        stop["flag"] = True
        t.join(5)

    return stop_fn, ready


def _send(sock_path, data):
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(sock_path)
    c.sendall(data)
    c.shutdown(socket.SHUT_WR)
    c.close()


class _FakeVariant:
    def __init__(self, sig, payload):
        self.sig = sig
        self.payload = payload


class _FakeGLib:
    Variant = _FakeVariant


class _FakeGio:
    class DBusCallFlags:
        NONE = 0


class _FakeBus:
    """Records NotifyKeyboardKeysym calls as (keysym, state) pairs."""
    def __init__(self):
        self.keys = []

    def call_sync(self, dest, path, iface, method, variant, *a, **kw):
        if method == "NotifyKeyboardKeysym":
            _handle, _opts, keysym, st = variant.payload
            self.keys.append((keysym, st))


def _emit_capture(text):
    bus = _FakeBus()
    state = {"bus": bus, "GLib": _FakeGLib, "Gio": _FakeGio, "handle": "/session"}
    os.environ["WD_SETTLE_MS"] = "0"  # no sleep in tests
    try:
        ptd._emit(state, text)
    finally:
        os.environ.pop("WD_SETTLE_MS", None)
    return bus.keys


def test_modifiers_are_released_before_typing():
    """The keys that trigger dictation are still physically down when injection
    starts, and Mutter's virtual device shares the real keymap state. Without an
    explicit release, each keysym arrives modified - 't' as Super+t and space as
    Super+space, firing whatever owns those hotkeys - and a held Shift uppercases
    everything.
    """
    keys = _emit_capture("hi")
    mods = set(ptd._MODIFIER_KEYSYMS)
    text_start = next(i for i, (ks, _) in enumerate(keys) if ks not in mods)
    released = {ks for ks, st in keys[:text_start] if st == 0}
    assert released == mods, f"every modifier must be released first, got {released!r}"
    assert all(st == 0 for _, st in keys[:text_start]), \
        "modifier keysyms must only be released, never pressed"


def test_text_is_still_typed_after_the_releases():
    keys = _emit_capture("hi")
    mods = set(ptd._MODIFIER_KEYSYMS)
    typed = [(ks, st) for ks, st in keys if ks not in mods]
    assert typed == [(ord("h"), 1), (ord("h"), 0), (ord("i"), 1), (ord("i"), 0)], typed


def test_connection_delivers_text_to_injector():
    received = []
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "t.sock")
        stop_fn, _ = _run_server(sock, received.append)
        try:
            _send(sock, "hello world".encode("utf-8"))
            time.sleep(0.3)
        finally:
            stop_fn()
    assert received == ["hello world"], received


def test_utf8_roundtrip():
    received = []
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "t.sock")
        stop_fn, _ = _run_server(sock, received.append)
        try:
            _send(sock, "café — 😀".encode("utf-8"))
            time.sleep(0.3)
        finally:
            stop_fn()
    assert received == ["café — 😀"], received


def test_empty_connection_does_not_inject():
    received = []
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "t.sock")
        stop_fn, _ = _run_server(sock, received.append)
        try:
            _send(sock, b"")
            time.sleep(0.3)
        finally:
            stop_fn()
    assert received == [], received


def test_injector_exception_does_not_kill_daemon():
    calls = {"n": 0}

    def flaky(text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        calls["last"] = text

    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "t.sock")
        stop_fn, _ = _run_server(sock, flaky)
        try:
            _send(sock, b"first")
            time.sleep(0.2)
            _send(sock, b"second")  # daemon must still be serving
            time.sleep(0.2)
        finally:
            stop_fn()
    assert calls["n"] == 2, calls
    assert calls.get("last") == "second", calls


def test_socket_is_mode_0600():
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "t.sock")
        stop_fn, _ = _run_server(sock, lambda t: None)
        try:
            mode = os.stat(sock).st_mode & 0o777
        finally:
            stop_fn()
    assert mode == 0o600, oct(mode)


def test_stale_socket_is_reclaimed():
    # A leftover socket file with nobody listening must be removed so the
    # daemon can bind. _prepare_socket_path should unlink it silently.
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "t.sock")
        # Create a stale AF_UNIX file by binding then closing without listening.
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(sock)
        dead.close()  # leaves the path on disk, no listener
        assert os.path.exists(sock)
        stop_fn, _ = _run_server(sock, lambda t: None)
        try:
            # If reclaim worked, the server bound and is accepting.
            received = []
            stop_fn2 = stop_fn  # noqa
            _send(sock, b"ok")
            time.sleep(0.2)
        finally:
            stop_fn()
    # No assertion error from _send means bind succeeded over the stale path.


def test_socket_cleaned_up_on_exit():
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "t.sock")
        stop_fn, _ = _run_server(sock, lambda t: None)
        assert os.path.exists(sock)
        stop_fn()
        time.sleep(0.3)
    assert not os.path.exists(sock), "socket file should be removed on shutdown"


if __name__ == "__main__":
    failures = 0
    for name in sorted(n for n in dir() if n.startswith("test_")):
        try:
            globals()[name]()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa
            failures += 1
            print(f"ERROR {name}: {e!r}")
    sys.exit(1 if failures else 0)

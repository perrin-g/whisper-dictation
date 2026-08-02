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


def _send_and_read_ack(sock_path, data):
    """Send like a real client and return the daemon's ack bytes."""
    c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    c.connect(sock_path)
    c.sendall(data)
    c.shutdown(socket.SHUT_WR)
    try:
        return c.recv(16)
    finally:
        c.close()


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


def test_successful_inject_acks_ok():
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "t.sock")
        stop_fn, _ = _run_server(sock, lambda t: None)
        try:
            ack = _send_and_read_ack(sock, b"hello")
        finally:
            stop_fn()
    assert ack.strip() == b"OK", ack


def test_failed_inject_acks_err():
    def boom(_text):
        raise RuntimeError("session dead")

    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "t.sock")
        stop_fn, _ = _run_server(sock, boom)
        try:
            ack = _send_and_read_ack(sock, b"hello")
        finally:
            stop_fn()
    assert ack.strip() == b"ERR", ack


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

#!/usr/bin/env python3
"""Unit tests for portal_type.py pure logic (no D-Bus / portal needed).

Covers the char->keysym mapping and asserts the device mask is keyboard-only.
"""
import os
import socket
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import portal_type as pt


def test_ascii_maps_to_codepoint():
    assert pt.char_to_keysym("a") == ord("a") == 0x61
    assert pt.char_to_keysym("Z") == ord("Z")
    assert pt.char_to_keysym(" ") == 0x20


def test_latin1_direct():
    # e-acute is within 0x20-0xFF -> direct
    assert pt.char_to_keysym("é") == 0xE9


def test_unicode_uses_offset():
    # em dash U+2014 is outside latin-1 -> 0x01000000 + codepoint
    assert pt.char_to_keysym("—") == 0x01000000 + 0x2014
    # an emoji
    assert pt.char_to_keysym("\U0001F600") == 0x01000000 + 0x1F600


def test_control_chars():
    assert pt.char_to_keysym("\n") == pt.KEYSYM_RETURN
    assert pt.char_to_keysym("\r") == pt.KEYSYM_RETURN
    assert pt.char_to_keysym("\t") == pt.KEYSYM_TAB


def test_text_to_keysyms_sequence():
    assert pt.text_to_keysyms("hi") == [ord("h"), ord("i")]
    assert pt.text_to_keysyms("") == []


def test_device_mask_is_keyboard_only():
    # 1 = KEYBOARD; must never include POINTER (2) or TOUCH (4).
    assert pt.DEVICE_KEYBOARD == 1
    assert pt.DEVICE_KEYBOARD & 2 == 0, "pointer must not be requested"
    assert pt.DEVICE_KEYBOARD & 4 == 0, "touch must not be requested"


def test_no_token_machinery():
    # GNOME RemoteDesktop returns no restore token, so the one-shot path must
    # not pretend to persist one. Persistence lives in the daemon instead.
    src = open(os.path.join(os.path.dirname(__file__), "portal_type.py")).read()
    assert "save_restore_token" not in src, "no token persistence in one-shot path"
    assert not os.path.exists(
        os.path.join(os.path.dirname(__file__), "restore_token")
    ), "no restore_token file should exist on disk"


def test_send_via_daemon_reports_unreachable_when_no_socket():
    with tempfile.TemporaryDirectory() as d:
        missing = os.path.join(d, "nope.sock")
        assert pt.send_via_daemon("hi", sock_path=missing) == "unreachable"


def _fake_daemon(sock, received, ack=b"OK"):
    """A listener that records what it was sent and acks like the real daemon."""
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(sock)
    srv.listen(1)

    def accept_one():
        conn, _ = srv.accept()
        chunks = []
        while True:
            b = conn.recv(4096)
            if not b:
                break
            chunks.append(b)
        received.append(b"".join(chunks).decode("utf-8"))
        if ack is not None:
            conn.sendall(ack)
        conn.close()

    t = threading.Thread(target=accept_one, daemon=True)
    t.start()
    time.sleep(0.1)
    return srv, t


def test_send_via_daemon_delivers_to_listener():
    received = []
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "t.sock")
        srv, t = _fake_daemon(sock, received)
        status = pt.send_via_daemon("café 😀", sock_path=sock)
        t.join(5)
        srv.close()
    assert status == "ok", status
    assert received == ["café 😀"], received


def test_send_via_daemon_reports_inject_failed_without_an_ok_ack():
    """A daemon that was reached but could not inject must not be read as success -
    the caller has to know the text did not land."""
    received = []
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "t.sock")
        srv, t = _fake_daemon(sock, received, ack=b"ERR")
        status = pt.send_via_daemon("hi", sock_path=sock)
        t.join(5)
        srv.close()
    assert status == "inject_failed", status


def test_send_via_daemon_reports_unreachable_on_stale_socket():
    # Socket file exists but nobody is listening -> connect fails. No fallback to
    # a one-shot session: that would silently re-prompt for consent.
    with tempfile.TemporaryDirectory() as d:
        sock = os.path.join(d, "t.sock")
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(sock)
        dead.close()  # path remains, no listener
        assert os.path.exists(sock)
        assert pt.send_via_daemon("hi", sock_path=sock) == "unreachable"


if __name__ == "__main__":
    failures = 0
    for name in sorted(n for n in dir() if n.startswith("test_")):
        try:
            globals()[name]()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
    sys.exit(1 if failures else 0)

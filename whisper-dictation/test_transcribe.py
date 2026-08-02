#!/usr/bin/env python3
"""Tests for transcribe.py.

- Ground-truth transcription against the known JFK sample.
- Error handling for a missing audio file.
- Checksum verification rejects a tampered model.
"""
import os
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(BASE, "venv", "bin", "python")
SCRIPT = os.path.join(BASE, "transcribe.py")
JFK = "/tmp/jfk.wav"


def run(args):
    return subprocess.run(
        [PY, SCRIPT, *args], capture_output=True, text=True
    )


def test_transcribes_known_sample():
    assert os.path.exists(JFK), "JFK sample missing - download it first"
    r = run([JFK])
    assert r.returncode == 0, f"non-zero exit: {r.stderr}"
    text = r.stdout.lower()
    # assert the distinctive content words are present
    for word in ["country", "americans", "ask"]:
        assert word in text, f"expected '{word}' in transcript, got: {text!r}"
    # no trailing newline (caller controls submission)
    assert not r.stdout.endswith("\n"), "transcript should not end with newline"
    print(f"  transcript: {r.stdout!r}")


def test_missing_file_errors():
    r = run(["/tmp/does-not-exist-12345.wav"])
    assert r.returncode != 0, "should exit non-zero on missing file"
    assert "not found" in r.stderr.lower()


def test_checksum_mismatch_rejected():
    """Point the script at a bad checksum file via a temp copy and confirm abort."""
    import hashlib
    import shutil

    # Build a temp dir mirroring layout with a deliberately wrong checksum.
    with tempfile.TemporaryDirectory() as td:
        # minimal: copy transcribe.py, make a model dir with one fake file
        shutil.copy(SCRIPT, os.path.join(td, "transcribe.py"))
        os.mkdir(os.path.join(td, "model"))
        fake = os.path.join(td, "model", "config.json")
        with open(fake, "w") as f:
            f.write("tampered")
        with open(os.path.join(td, "model.sha256"), "w") as f:
            f.write("0" * 64 + "  config.json\n")
        r = subprocess.run(
            [PY, os.path.join(td, "transcribe.py"), JFK],
            capture_output=True, text=True,
        )
        assert r.returncode != 0, "should abort on checksum mismatch"
        assert "mismatch" in r.stderr.lower()


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

#!/usr/bin/env python3
"""Transcribe a 16 kHz mono WAV with faster-whisper small.en.

Verifies the model files against the recorded checksums before loading,
then prints the transcript to stdout. Cold-loads the model per invocation
(no resident daemon) - costs a few seconds but holds no idle RAM.
"""
import hashlib
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR = os.path.join(BASE, "model")
CHECKSUM_FILE = os.path.join(BASE, "model.sha256")


def verify_model():
    """Check every recorded file matches its SHA-256, else abort.

    A tampered or truncated model is a security/correctness risk, so we
    refuse to load rather than transcribe with unknown weights.
    """
    if not os.path.exists(CHECKSUM_FILE):
        sys.exit(f"checksum file missing: {CHECKSUM_FILE}")
    with open(CHECKSUM_FILE) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            expected, name = line.split(maxsplit=1)
            path = os.path.join(MODEL_DIR, name)
            if not os.path.exists(path):
                sys.exit(f"model file missing: {name}")
            h = hashlib.sha256()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != expected:
                sys.exit(f"checksum mismatch for {name} - refusing to load")


def transcribe(wav_path):
    if not os.path.exists(wav_path):
        sys.exit(f"audio file not found: {wav_path}")
    from faster_whisper import WhisperModel

    model = WhisperModel(MODEL_DIR, device="cpu", compute_type="int8")
    segments, _ = model.transcribe(wav_path, language="en")
    text = " ".join(seg.text.strip() for seg in segments).strip()
    # Whisper .en emits US spelling; rewrite to en-NZ deterministically.
    from en_nz import to_nz

    return to_nz(text)


def main(argv):
    if len(argv) != 2:
        sys.exit("usage: transcribe.py <wav-file>")
    verify_model()
    text = transcribe(argv[1])
    # print without trailing newline so callers control submission behaviour
    sys.stdout.write(text)


if __name__ == "__main__":
    main(sys.argv)

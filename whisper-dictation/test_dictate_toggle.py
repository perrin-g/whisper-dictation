#!/usr/bin/env python3
"""State-machine tests for dictate-toggle using stub commands (no audio/portal).

Drives the script with WD_* env overrides pointed at fake record/transcribe/type
commands writing to a sandbox $XDG_RUNTIME_DIR, then asserts the resulting state.
"""
import os
import subprocess
import sys
import tempfile

TOGGLE = os.path.expanduser("~/.local/bin/dictate-toggle")


def make_env(runtime, record="record", transcribe="transcribe", typer="typer"):
    env = dict(os.environ)
    env["XDG_RUNTIME_DIR"] = runtime
    # stub record: writes some bytes to the target wav (last arg)
    env["WD_RECORD_CMD"] = f"bash {record}"
    env["WD_TRANSCRIBE_CMD"] = f"bash {transcribe}"
    env["WD_TYPE_CMD"] = f"bash {typer}"
    env["WD_NOTIFY_CMD"] = "true"  # swallow notifications
    # force "not muted" by default so the mute guard never blocks these tests
    # (the real wpctl would report the tester's actual mic state); the mute
    # test overrides this back to a MUTED-reporting stub.
    env["WD_MUTE_CHECK_CMD"] = "true"  # prints nothing -> grep MUTED fails
    return env


def write_stubs(d, *, record_writes=b"AUDIODATA", transcript="hello world"):
    record = os.path.join(d, "record")
    transcribe = os.path.join(d, "transcribe")
    typer = os.path.join(d, "typer")
    typed_out = os.path.join(d, "typed.txt")
    # record: $1 is the wav path; write bytes and sleep so it stays "running"
    with open(record, "w") as f:
        f.write(f'#!/usr/bin/env bash\nprintf %s {record_writes.decode()!r} > "$1"\nsleep 30\n')
    # transcribe: $1 is wav; emit the canned transcript verbatim (incl. any
    # real newlines) by cat-ing a fixture file, so escapes aren't mangled.
    fixture = os.path.join(d, "transcript.txt")
    with open(fixture, "w") as f:
        f.write(transcript)
    with open(transcribe, "w") as f:
        f.write(f'#!/usr/bin/env bash\ncat {fixture!r}\n')
    # typer: read stdin, append to typed.txt (proves what got "typed")
    with open(typer, "w") as f:
        f.write(f'#!/usr/bin/env bash\ncat >> {typed_out!r}\n')
    for p in (record, transcribe, typer):
        os.chmod(p, 0o755)
    return record, transcribe, typer, typed_out


def run_toggle(env, timeout=15):
    return subprocess.run([TOGGLE], env=env, capture_output=True, text=True,
                          timeout=timeout)


def test_first_press_starts_recording():
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        r = run_toggle(env)
        assert r.returncode == 0, r.stderr
        assert os.path.exists(os.path.join(rt, "dictate.pid")), "PID file should exist"
        # the backgrounded recorder may take a moment to create the wav
        import time
        wav = os.path.join(rt, "dictate.wav")
        for _ in range(50):
            if os.path.exists(wav):
                break
            time.sleep(0.1)
        assert os.path.exists(wav), "wav should be recording"
        # cleanup the lingering stub recorder
        with open(os.path.join(rt, "dictate.pid")) as f:
            try:
                os.kill(int(f.read()), 9)
            except ProcessLookupError:
                pass


def test_second_press_transcribes_types_and_cleans_up():
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, typed = write_stubs(sd, transcript="the quick brown fox")
        env = make_env(rt, rec, tr, ty)
        run_toggle(env)                      # start
        r = run_toggle(env)                  # stop + transcribe + type
        assert r.returncode == 0, r.stderr
        assert not os.path.exists(os.path.join(rt, "dictate.pid")), "PID file cleaned"
        assert not os.path.exists(os.path.join(rt, "dictate.wav")), "wav deleted"
        with open(typed) as f:
            assert f.read() == "the quick brown fox", "typed text must match transcript"


def test_trailing_newline_stripped():
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, typed = write_stubs(sd, transcript="line one\n")
        env = make_env(rt, rec, tr, ty)
        run_toggle(env)
        run_toggle(env)
        with open(typed) as f:
            out = f.read()
        assert out == "line one", f"trailing newline must be stripped, got {out!r}"


def test_recorder_does_not_hold_lock():
    """Regression: the backgrounded recorder must NOT inherit the flock FD.

    If it does, the second press blocks forever waiting on the lock. The stub
    recorder sleeps 30s; the lock file must be acquirable while it 'records'.
    """
    import fcntl
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        run_toggle(env)  # start recording (stub sleeps 30s in background)
        try:
            # the lock must be free while recording is in progress
            lock_path = os.path.join(rt, "dictate.lock")
            fd = os.open(lock_path, os.O_WRONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # raises if held
            finally:
                os.close(fd)
        finally:
            with open(os.path.join(rt, "dictate.pid")) as f:
                try:
                    os.kill(int(f.read()), 9)
                except ProcessLookupError:
                    pass


def test_press_during_transcribe_is_ignored():
    """A press while the lock is held (transcribe in progress) is dropped,
    not queued - prevents stacking. We hold the lock and confirm toggle exits
    fast without blocking."""
    import fcntl
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        os.makedirs(rt, exist_ok=True)
        lock_path = os.path.join(rt, "dictate.lock")
        fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)  # hold it like an in-flight transcribe
        try:
            r = run_toggle(env, timeout=5)  # must NOT block on the held lock
            assert r.returncode == 0, r.stderr
            # nothing should have started while busy
            assert not os.path.exists(os.path.join(rt, "dictate.pid"))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def test_muted_mic_warns_and_does_not_record():
    """First press with a muted mic must warn and NOT start recording."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        env["WD_MUTE_CHECK_CMD"] = "echo Volume: 1.00 [MUTED]"
        r = run_toggle(env)
        assert r.returncode == 0, r.stderr
        assert not os.path.exists(os.path.join(rt, "dictate.pid")), \
            "muted mic must not start recording"
        assert not os.path.exists(os.path.join(rt, "dictate.wav")), \
            "muted mic must not create a wav"


def test_empty_audio_handled():
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        # record writes nothing -> empty wav
        rec, tr, ty, typed = write_stubs(sd, record_writes=b"")
        env = make_env(rt, rec, tr, ty)
        run_toggle(env)
        r = run_toggle(env)
        assert r.returncode == 0, r.stderr
        assert not os.path.exists(os.path.join(rt, "dictate.wav")), "empty wav removed"
        assert not os.path.exists(typed), "nothing should be typed for empty audio"


if __name__ == "__main__":
    failures = 0
    for name in sorted(n for n in dir() if n.startswith("test_")):
        try:
            globals()[name]()
            print(f"PASS {name}")
        except AssertionError as e:
            failures += 1
            print(f"FAIL {name}: {e}")
        except Exception as e:
            failures += 1
            print(f"ERROR {name}: {e!r}")
    sys.exit(1 if failures else 0)

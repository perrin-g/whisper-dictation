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
    # no source probe by default: pw-dump isn't available in a test sandbox, and
    # an empty pick means "use the PipeWire default", the pre-picker behaviour
    env["WD_PICK_SOURCE_CMD"] = "true"
    # force "not muted" by default so the mute guard never blocks these tests
    # (the real wpctl would report the tester's actual mic state); the mute
    # test overrides this back to a MUTED-reporting stub.
    env["WD_MUTE_CHECK_CMD"] = "true"  # prints nothing -> grep MUTED fails
    # pretend daemon is always alive so tests don't depend on a real socket
    env["WD_DAEMON_CHECK_CMD"] = "true"
    # Never touch the real clipboard or the real service from a test: wl-copy
    # would overwrite whatever the tester had copied, and systemctl would raise
    # a GNOME consent prompt mid-run.
    env["WD_CLIP_CMD"] = "true"
    env["WD_DAEMON_START_CMD"] = "true"
    return env


def write_stubs(d, *, record_writes=b"AUDIODATA", transcript="hello world"):
    record = os.path.join(d, "record")
    transcribe = os.path.join(d, "transcribe")
    typer = os.path.join(d, "typer")
    typed_out = os.path.join(d, "typed.txt")
    # record: the wav path is the LAST arg (as with real pw-record, which takes
    # options such as --target before the file); log argv so tests can assert
    # what the recorder was invoked with. Sleep so it stays "running".
    args_out = os.path.join(d, "record_args.txt")
    with open(record, "w") as f:
        f.write(f'#!/usr/bin/env bash\n'
                f'printf "%s\\n" "$*" >> {args_out!r}\n'
                f'printf %s {record_writes.decode()!r} > "${{@: -1}}"\n'
                f'sleep 30\n')
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


def test_daemon_down_starts_service_and_does_not_record():
    """Daemon down: the press starts the service and records nothing. The consent
    prompt has to be approved first, so there is nothing to record into yet."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        env["WD_DAEMON_CHECK_CMD"] = "false"  # daemon not alive
        started = os.path.join(sd, "started.txt")
        env["WD_DAEMON_START_CMD"] = write_marker(sd, "start_daemon", started)
        r = run_toggle(env)
        assert r.returncode == 0, r.stderr
        assert wait_for(started), "daemon down must trigger a service start"
        assert not os.path.exists(os.path.join(rt, "dictate.pid")), \
            "daemon down must not start recording"
        assert not os.path.exists(os.path.join(rt, "dictate.wav")), \
            "daemon down must not create a wav"


def test_press_after_daemon_comes_up_records():
    """The second press, once the daemon is alive, records normally - there is no
    wait-and-retry inside a single press, the user presses again."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        env["WD_DAEMON_CHECK_CMD"] = "false"
        run_toggle(env)  # daemon down: starts the service, records nothing
        assert not os.path.exists(os.path.join(rt, "dictate.pid"))
        env["WD_DAEMON_CHECK_CMD"] = "true"  # consent approved, daemon now alive
        r = run_toggle(env)
        assert r.returncode == 0, r.stderr
        try:
            assert os.path.exists(os.path.join(rt, "dictate.pid")), \
                "press with a live daemon must start recording"
        finally:
            _kill_recorder(rt)


def test_stale_empty_pidfile_starts_fresh_recording():
    """Stale PIDFILE with empty content (e.g. after session lock) cleans up and
    starts a new recording instead of routing to stop_and_transcribe."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        # Plant a stale PIDFILE with empty content and a stale wav
        pidfile = os.path.join(rt, "dictate.pid")
        wav = os.path.join(rt, "dictate.wav")
        with open(pidfile, "w") as f:
            f.write("")
        with open(wav, "wb") as f:
            f.write(b"staleaudio")
        r = run_toggle(env)
        assert r.returncode == 0, r.stderr
        # Stale wav must be deleted, new recording must have started (new PIDFILE)
        assert not os.path.exists(wav) or os.path.getsize(wav) != 10, \
            "stale wav should be replaced by new recording"
        assert os.path.exists(pidfile), "new recording should have written a PIDFILE"
        # cleanup lingering stub recorder
        with open(pidfile) as f:
            try:
                os.kill(int(f.read()), 9)
            except (ProcessLookupError, ValueError):
                pass


def test_stale_dead_pid_starts_fresh_recording():
    """Stale PIDFILE with a dead PID (e.g. pw-record crashed) cleans up and
    starts a new recording."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        pidfile = os.path.join(rt, "dictate.pid")
        wav = os.path.join(rt, "dictate.wav")
        with open(pidfile, "w") as f:
            f.write("999999\n")  # PID that will never exist
        with open(wav, "wb") as f:
            f.write(b"staleaudio")
        r = run_toggle(env)
        assert r.returncode == 0, r.stderr
        assert os.path.exists(pidfile), "new recording should have written a PIDFILE"
        pid_content = open(pidfile).read().strip()
        assert pid_content != "999999", "PIDFILE must hold new PID, not the stale one"
        # cleanup
        with open(pidfile) as f:
            try:
                os.kill(int(f.read()), 9)
            except (ProcessLookupError, ValueError):
                pass


def write_picker(d, line):
    """A pick-source stub printing one <id>\\t<name>\\t<desc> line (or nothing).

    The line is cat-ed from a fixture so real tabs survive (printf %s would keep
    a literal backslash-t and the caller's cut -f would see one field).
    """
    fixture = os.path.join(d, "picked.txt")
    with open(fixture, "w") as f:
        f.write(line)
    p = os.path.join(d, "picker")
    with open(p, "w") as f:
        f.write(f'#!/usr/bin/env bash\ncat {fixture!r}\n')
    os.chmod(p, 0o755)
    return f"bash {p}"


def write_marker(d, name, marker):
    """A stub that records the fact it ran, for asserting a command was invoked."""
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write(f'#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> {marker!r}\n')
    os.chmod(p, 0o755)
    return f"bash {p}"


def write_failing_typer(d):
    """A typer stub that drains stdin and fails, like a dead portal session."""
    p = os.path.join(d, "typer_fail")
    with open(p, "w") as f:
        f.write('#!/usr/bin/env bash\ncat >/dev/null\nexit 1\n')
    os.chmod(p, 0o755)
    return f"bash {p}"


def write_clip(d):
    """A wl-copy stub that records what it was handed."""
    out = os.path.join(d, "clipboard.txt")
    p = os.path.join(d, "clip")
    with open(p, "w") as f:
        f.write(f'#!/usr/bin/env bash\ncat >> {out!r}\n')
    os.chmod(p, 0o755)
    return f"bash {p}", out


def wait_for(path, timeout=5.0):
    """The recorder is backgrounded; give it a moment to write its side effects."""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.05)
    return False


def write_mute_check(d, muted_id):
    """A wpctl stub reporting MUTED only for the given node id ($1)."""
    p = os.path.join(d, "mute_check")
    with open(p, "w") as f:
        f.write(f'#!/usr/bin/env bash\n[ "$1" = {muted_id!r} ] && echo "Volume: 1.00 [MUTED]"\n'
                f'exit 0\n')
    os.chmod(p, 0o755)
    return f"bash {p}"


def _kill_recorder(rt):
    pidfile = os.path.join(rt, "dictate.pid")
    if not os.path.exists(pidfile):
        return
    with open(pidfile) as f:
        try:
            os.kill(int(f.read()), 9)
        except (ProcessLookupError, ValueError):
            pass


def test_recorder_is_pinned_to_picked_source():
    """The mic chosen by pick-source must be passed to the recorder as --target,
    so dictation uses the headset even when it isn't the system default."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        env["WD_PICK_SOURCE_CMD"] = write_picker(sd, "77\tbluez_input.AA\tHeadset X")
        r = run_toggle(env)
        assert r.returncode == 0, r.stderr
        args_file = os.path.join(sd, "record_args.txt")
        try:
            assert wait_for(args_file), "recorder never ran"
            args = open(args_file).read()
            assert "--target bluez_input.AA" in args, \
                f"recorder must be pinned to the picked source, got {args!r}"
        finally:
            _kill_recorder(rt)


def test_no_pick_falls_back_to_default_source():
    """With no capture device found (or a failed probe) the recorder must run
    untargeted, i.e. follow PipeWire's default source as it did before."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        env["WD_PICK_SOURCE_CMD"] = "false"  # probe fails, prints nothing
        r = run_toggle(env)
        assert r.returncode == 0, r.stderr
        args_file = os.path.join(sd, "record_args.txt")
        try:
            assert os.path.exists(os.path.join(rt, "dictate.pid")), \
                "a failed source probe must not stop dictation"
            assert wait_for(args_file), "recorder never ran"
            args = open(args_file).read()
            assert "--target" not in args, f"expected no target, got {args!r}"
        finally:
            _kill_recorder(rt)


def test_mute_check_targets_picked_node():
    """The mute guard must test the picked mic, not the system default - the
    headset can be live while the default source sits muted, and vice versa."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        env["WD_PICK_SOURCE_CMD"] = write_picker(sd, "77\tbluez_input.AA\tHeadset X")
        # reports MUTED only when asked about node 77 (the picked one)
        env["WD_MUTE_CHECK_CMD"] = write_mute_check(sd, "77")
        r = run_toggle(env)
        assert r.returncode == 0, r.stderr
        try:
            assert not os.path.exists(os.path.join(rt, "dictate.pid")), \
                "muted picked mic must not start recording"
        finally:
            _kill_recorder(rt)


def test_successful_type_stashes_transcript():
    """The transcript is kept for --retype, at mode 0600 - it is dictated speech."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd, transcript="mind the gap")
        env = make_env(rt, rec, tr, ty)
        run_toggle(env)
        r = run_toggle(env)
        assert r.returncode == 0, r.stderr
        last = os.path.join(rt, "dictate.last")
        assert os.path.exists(last), "transcript must be stashed for retype"
        assert open(last).read() == "mind the gap"
        mode = os.stat(last).st_mode & 0o777
        assert mode == 0o600, f"stash must be 0600, got {oct(mode)}"


def test_failed_type_stashes_and_copies_to_clipboard():
    """The failure this whole feature exists for: the inject fails, and the words
    survive in both the stash and the clipboard."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd, transcript="do not lose this")
        clip, clip_out = write_clip(sd)
        env = make_env(rt, rec, tr, ty)
        env["WD_TYPE_CMD"] = write_failing_typer(sd)
        env["WD_CLIP_CMD"] = clip
        run_toggle(env)
        r = run_toggle(env)
        assert r.returncode == 0, r.stderr
        last = os.path.join(rt, "dictate.last")
        assert os.path.exists(last), "failed type must still leave a stash"
        assert open(last).read() == "do not lose this"
        assert wait_for(clip_out), "failed type must put the text on the clipboard"
        assert open(clip_out).read() == "do not lose this"


def test_retype_types_the_stashed_text():
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, typed = write_stubs(sd, transcript="second time lucky")
        env = make_env(rt, rec, tr, ty)
        run_toggle(env)
        run_toggle(env)          # types once
        os.remove(typed)         # forget that, we care about the retype
        r = subprocess.run([TOGGLE, "--retype"], env=env, capture_output=True,
                           text=True, timeout=15)
        assert r.returncode == 0, r.stderr
        assert open(typed).read() == "second time lucky"


def test_retype_keeps_the_stash_for_another_attempt():
    """A retype can land in the wrong window too, so the stash must survive it."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, typed = write_stubs(sd, transcript="again please")
        env = make_env(rt, rec, tr, ty)
        run_toggle(env)
        run_toggle(env)
        subprocess.run([TOGGLE, "--retype"], env=env, capture_output=True, timeout=15)
        assert os.path.exists(os.path.join(rt, "dictate.last")), \
            "stash must survive a retype"
        subprocess.run([TOGGLE, "--retype"], env=env, capture_output=True, timeout=15)
        assert open(typed).read() == "again please" * 3, \
            "a second retype must work as well"


def test_retype_with_no_stash_types_nothing():
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, typed = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        r = subprocess.run([TOGGLE, "--retype"], env=env, capture_output=True,
                           text=True, timeout=15)
        assert r.returncode == 0, r.stderr
        assert not os.path.exists(typed), "nothing stashed means nothing typed"


def test_retype_during_recording_leaves_the_recording_alone():
    """Retype must be safe to press mid-recording: it touches no recorder state."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd, transcript="earlier text")
        env = make_env(rt, rec, tr, ty)
        run_toggle(env)
        run_toggle(env)          # completed dictation, stash populated
        run_toggle(env)          # start a new recording
        pidfile = os.path.join(rt, "dictate.pid")
        assert os.path.exists(pidfile)
        before = open(pidfile).read()
        try:
            r = subprocess.run([TOGGLE, "--retype"], env=env, capture_output=True,
                               text=True, timeout=15)
            assert r.returncode == 0, r.stderr
            assert os.path.exists(pidfile), "retype must not end the recording"
            assert open(pidfile).read() == before, "retype must not touch the PID"
        finally:
            _kill_recorder(rt)


def test_unknown_option_is_rejected():
    """A typo in the hotkey command must fail loudly, not silently record."""
    with tempfile.TemporaryDirectory() as rt, tempfile.TemporaryDirectory() as sd:
        rec, tr, ty, _ = write_stubs(sd)
        env = make_env(rt, rec, tr, ty)
        r = subprocess.run([TOGGLE, "--retpye"], env=env, capture_output=True,
                           text=True, timeout=15)
        assert r.returncode == 2, f"expected exit 2, got {r.returncode}"
        assert not os.path.exists(os.path.join(rt, "dictate.pid")), \
            "a bad option must not start recording"


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

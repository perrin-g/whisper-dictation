#!/usr/bin/env python3
"""Ranking tests for pick-source using synthetic pw-dump JSON (no real audio).

Each test builds a fake PipeWire graph, feeds it in via WD_PW_DUMP_CMD, and
asserts which mic wins. The point being protected: any headset beats the
built-in mic, and the built-in mic is always a working fallback.
"""
import json
import os
import subprocess
import sys
import tempfile

PICKER = os.path.expanduser("~/.local/share/whisper-dictation/pick-source")


def device(id, *, bus=None, form_factor=None, name=None, desc=None):
    props = {"device.name": name or f"card_{id}", "device.description": desc or f"Device {id}"}
    if bus:
        props["device.bus"] = bus
    if form_factor:
        props["device.form-factor"] = form_factor
    return {"id": id, "type": "PipeWire:Interface:Device", "info": {"props": props}}


def source(id, name, desc, *, device_id=None, cls="Audio/Source", serial=None):
    props = {"node.name": name, "node.description": desc, "media.class": cls,
             "object.serial": serial if serial is not None else id}
    if device_id is not None:
        props["device.id"] = device_id
    return {"id": id, "type": "PipeWire:Interface:Node", "info": {"props": props}}


INTERNAL_DEV = device(53, bus="pci")
INTERNAL_SRC = source(47, "alsa_input.pci-0000_c3_00.6.analog-stereo",
                      "Ryzen HD Audio Controller Analog Stereo", device_id=53)


def pick(graph, prefer=None):
    """Run the picker over a synthetic graph; returns the printed line."""
    with tempfile.TemporaryDirectory() as d:
        dump = os.path.join(d, "dump.json")
        with open(dump, "w") as f:
            json.dump(graph, f)
        stub = os.path.join(d, "pw-dump")
        with open(stub, "w") as f:
            f.write(f'#!/usr/bin/env bash\ncat {dump!r}\n')
        os.chmod(stub, 0o755)
        env = dict(os.environ)
        env["WD_PW_DUMP_CMD"] = f"bash {stub}"
        args = [PICKER] + ([prefer] if prefer is not None else [])
        r = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()


def picked_name(line):
    return line.split("\t")[1] if line else ""


def test_bluetooth_headset_beats_internal_mic():
    graph = [INTERNAL_DEV, INTERNAL_SRC,
             device(78, bus="bluetooth", form_factor="headset"),
             source(80, "bluez_input.AA:BB:CC:DD:EE:FF", "BT Headset", device_id=78)]
    assert picked_name(pick(graph)) == "bluez_input.AA:BB:CC:DD:EE:FF"


def test_usb_headset_beats_internal_mic():
    graph = [INTERNAL_DEV, INTERNAL_SRC,
             device(60, bus="usb", form_factor="headset"),
             source(61, "alsa_input.usb-Jabra_Evolve", "Jabra Evolve Mono", device_id=60)]
    assert picked_name(pick(graph)) == "alsa_input.usb-Jabra_Evolve"


def test_usb_mic_without_form_factor_still_beats_internal():
    """A USB mic/dock that reports no form-factor must still outrank the
    built-in mic - form-factor is frequently absent."""
    graph = [INTERNAL_DEV, INTERNAL_SRC,
             device(60, bus="usb"),
             source(61, "alsa_input.usb-Generic_Headset", "Generic Headset", device_id=60)]
    assert picked_name(pick(graph)) == "alsa_input.usb-Generic_Headset"


def test_internal_mic_is_the_fallback():
    assert picked_name(pick([INTERNAL_DEV, INTERNAL_SRC])) == \
        "alsa_input.pci-0000_c3_00.6.analog-stereo"


def test_headset_paired_without_mic_does_not_win():
    """A Bluetooth headset in playback-only (A2DP) profile exposes a sink but no
    source node, so the built-in mic must still be picked."""
    graph = [INTERNAL_DEV, INTERNAL_SRC,
             device(78, bus="bluetooth", form_factor="headset"),
             source(85, "bluez_output.AA:BB:CC:DD:EE:FF", "BT Headset",
                    device_id=78, cls="Audio/Sink")]
    assert picked_name(pick(graph)) == "alsa_input.pci-0000_c3_00.6.analog-stereo"


def test_webcam_mic_ranks_below_internal():
    graph = [INTERNAL_DEV, INTERNAL_SRC,
             device(48, bus="usb", form_factor="webcam"),
             source(50, "alsa_input.usb-HP_5MP_Camera", "HP 5MP Camera", device_id=48)]
    assert picked_name(pick(graph)) == "alsa_input.pci-0000_c3_00.6.analog-stereo"


def test_monitor_source_is_never_picked():
    graph = [source(90, "alsa_output.pci-0000_c3_00.6.analog-stereo.monitor",
                    "Monitor of Analog Stereo", cls="Audio/Source"),
             INTERNAL_DEV, INTERNAL_SRC]
    assert picked_name(pick(graph)) == "alsa_input.pci-0000_c3_00.6.analog-stereo"


def test_stream_nodes_are_ignored():
    """The bluez loopback's internal capture stream is not a mic."""
    graph = [INTERNAL_DEV, INTERNAL_SRC,
             source(82, "bluez_capture_internal.AA:BB:CC:DD:EE:FF", "internal",
                    cls="Stream/Input/Audio/Internal")]
    assert picked_name(pick(graph)) == "alsa_input.pci-0000_c3_00.6.analog-stereo"


def test_two_headsets_newest_wins():
    graph = [INTERNAL_DEV, INTERNAL_SRC,
             device(78, bus="bluetooth", form_factor="headset"),
             source(80, "bluez_input.OLD", "Old Headset", device_id=78, serial=100),
             device(79, bus="bluetooth", form_factor="headset"),
             source(81, "bluez_input.NEW", "New Headset", device_id=79, serial=200)]
    assert picked_name(pick(graph)) == "bluez_input.NEW"


def test_preferred_name_wins_when_present():
    graph = [INTERNAL_DEV, INTERNAL_SRC,
             device(78, bus="bluetooth", form_factor="headset"),
             source(80, "bluez_input.AA:BB:CC:DD:EE:FF", "BT Headset", device_id=78)]
    got = picked_name(pick(graph, prefer="alsa_input.pci-0000_c3_00.6.analog-stereo"))
    assert got == "alsa_input.pci-0000_c3_00.6.analog-stereo", got


def test_preferred_name_absent_falls_back_to_ranking():
    graph = [INTERNAL_DEV, INTERNAL_SRC,
             device(78, bus="bluetooth", form_factor="headset"),
             source(80, "bluez_input.AA:BB:CC:DD:EE:FF", "BT Headset", device_id=78)]
    got = picked_name(pick(graph, prefer="alsa_input.usb-Headset_That_Is_Unplugged"))
    assert got == "bluez_input.AA:BB:CC:DD:EE:FF", got


def test_empty_prefer_argument_is_harmless():
    """dictate-toggle passes "" when WD_SOURCE is unset."""
    assert picked_name(pick([INTERNAL_DEV, INTERNAL_SRC], prefer="")) == \
        "alsa_input.pci-0000_c3_00.6.analog-stereo"


def test_no_capture_device_prints_nothing():
    assert pick([INTERNAL_DEV]) == ""


def test_id_and_description_are_emitted():
    """The caller needs the node id (for wpctl) and a human-readable name."""
    line = pick([INTERNAL_DEV, INTERNAL_SRC])
    parts = line.split("\t")
    assert parts[0] == "47", parts
    assert parts[2] == "Ryzen HD Audio Controller Analog Stereo", parts


def test_broken_probe_prints_nothing_and_succeeds():
    """A pw-dump that fails must not make the picker fail - the caller treats
    empty output as "use the default source"."""
    env = dict(os.environ)
    env["WD_PW_DUMP_CMD"] = "false"
    r = subprocess.run([PICKER], env=env, capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "", r.stdout


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

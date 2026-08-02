#!/usr/bin/env python3
"""Tests for the US -> en-NZ spelling pass."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import en_nz


def test_basic_our():
    assert en_nz.to_nz("the color of honor") == "the colour of honour"


def test_re_ending():
    assert en_nz.to_nz("center of the theater") == "centre of the theatre"


def test_ise_ending():
    assert en_nz.to_nz("organize and realize") == "organise and realise"
    assert en_nz.to_nz("analyze the data") == "analyse the data"


def test_case_preserved():
    assert en_nz.to_nz("Color") == "Colour"
    assert en_nz.to_nz("COLOR") == "COLOUR"
    assert en_nz.to_nz("color") == "colour"


def test_whole_words_only():
    # "colorful" is mapped, but a word that merely contains "color" as a
    # substring of a larger un-mapped token must not be touched.
    assert en_nz.to_nz("colorimetric") == "colorimetric"
    # "centered" maps; "decentered" (not in map) stays.
    assert en_nz.to_nz("recenter") == "recenter"


def test_no_false_positive_on_safe_words():
    # These look suffix-similar but must NOT change: size/prize/seize (-ize),
    # doctor/error/mirror (-or), here/sphere (-re-ish).
    for w in ["size", "prize", "seize", "doctor", "error", "mirror",
              "here", "sphere", "wire", "fire"]:
        assert en_nz.to_nz(w) == w, w


def test_ambiguous_words_left_alone():
    # "check the box" must stay - not become "cheque the box".
    assert en_nz.to_nz("check the box") == "check the box"
    assert en_nz.to_nz("tire") == "tire"
    assert en_nz.to_nz("curb your enthusiasm") == "curb your enthusiasm"


def test_sentence_mix():
    src = "I will organize the labor and recognize the color of the center."
    exp = "I will organise the labour and recognise the colour of the centre."
    assert en_nz.to_nz(src) == exp


def test_empty():
    assert en_nz.to_nz("") == ""


def test_doubled_l_and_misc():
    assert en_nz.to_nz("traveling and canceled") == "travelling and cancelled"
    assert en_nz.to_nz("gray aluminum") == "grey aluminium"


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

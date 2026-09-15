"""Library tests. Run: python3 -m pytest tests -q"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib import assemble as asm          # noqa: E402
from lib import audio as au              # noqa: E402
from lib import profile as pf            # noqa: E402
from lib import transcript as tr         # noqa: E402

STRUCTURED = """SCENE 1 — OPENING
[VISUAL: title]

First scene body text here.

SCENE 2 — MIDDLE
[VISUAL: diagram]

Second scene body.

More of the second scene.
"""

PROSE = "\n\n".join(f"Paragraph {i} with a handful of words." for i in range(1, 10))


# ── transcript ──

def test_structured_detected():
    secs, structured = tr.parse(STRUCTURED)
    assert structured and len(secs) == 2
    assert secs[0].title == "OPENING"
    assert secs[0].cue.startswith("VISUAL")


def test_cue_excluded_from_spoken_text():
    secs, _ = tr.parse(STRUCTURED)
    assert "VISUAL" not in secs[0].text


def test_prose_has_no_markers():
    secs, structured = tr.parse(PROSE)
    assert not structured and len(secs) == 9


def test_fit_to_count_hits_exact_count():
    for n in range(1, 10):
        assert len(tr.fit_to_count(PROSE, n)) == n


def test_fit_to_count_caps_at_paragraph_count():
    assert len(tr.fit_to_count(PROSE, 50)) == 9


def test_structured_never_regrouped():
    # Markers are a deliberate statement of structure; honouring a detected scene
    # count over them would silently discard the author's intent.
    assert len(tr.fit_to_count(STRUCTURED, 7)) == 2


def test_round_trip_is_stable():
    secs, _ = tr.parse(STRUCTURED)
    once = tr.render(secs)
    twice = tr.render(tr.parse(once)[0])
    assert once == twice


def test_fingerprint_ignores_whitespace_but_not_words():
    a, _ = tr.parse("SCENE 1 — X\n\nsome words here\n")
    b, _ = tr.parse("SCENE 1 — X\n\nsome    words\nhere\n")
    c, _ = tr.parse("SCENE 1 — X\n\nsome words there\n")
    assert a[0].fingerprint == b[0].fingerprint
    assert a[0].fingerprint != c[0].fingerprint


def test_replace_section_isolates_change():
    secs, _ = tr.parse(STRUCTURED)
    out = tr.replace_section(secs, 2, "Totally new.")
    assert out[1].text == "Totally new."
    assert out[0].text == secs[0].text
    assert out[1].title == secs[1].title and out[1].cue == secs[1].cue


def test_empty_transcript():
    assert tr.parse("") == ([], False)


# ── profile ──

def test_even_split_covers_whole_duration():
    sc = pf.scenes_even(100.0, 4)
    assert len(sc) == 4 and sc[0].start == 0 and abs(sc[-1].end - 100.0) < 0.01


def test_weighted_split_is_proportional():
    sc = pf.scenes_from_weights(100.0, [1, 3])
    assert abs(sc[0].duration - 25.0) < 0.1


def test_short_scenes_merged():
    scenes = pf._merge_short(
        [pf.Scene(1, 0, 10), pf.Scene(2, 10, 11), pf.Scene(3, 11, 30)], 30)
    assert len(scenes) == 2
    assert scenes[0].end == 11        # the 1s fragment folded backwards


def test_trailing_fragment_merged():
    scenes = pf._merge_short([pf.Scene(1, 0, 20), pf.Scene(2, 20, 21)], 21)
    assert len(scenes) == 1 and scenes[0].end == 21


def test_split_and_remove_are_inverse():
    base = pf.scenes_even(60.0, 2)
    split = pf.split_at(base, 15.0)
    assert len(split) == 3
    back = pf.remove_boundary(split, 2)
    assert len(back) == 2


def test_remove_boundary_rejects_first_scene():
    base = pf.scenes_even(60.0, 3)
    assert len(pf.remove_boundary(base, 1)) == 3


def test_scene_duration_never_negative():
    assert pf.Scene(1, 10.0, 5.0).duration == 0.0


# ── audio ──

def test_level_verdicts():
    assert au.level_verdict(-12.0, 5.0)[0] == "ok"
    assert au.level_verdict(-40.0, 5.0)[0] == "bad"
    assert au.level_verdict(-0.2, 5.0)[0] == "warn"
    assert au.level_verdict(-12.0, 0.2)[0] == "bad"


def test_process_trims_silence_and_normalises(tmp_path):
    raw = tmp_path / "raw.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=6",
                    "-af", "adelay=2000|2000,apad=pad_dur=2",
                    "-ar", "48000", "-ac", "1", str(raw)], check=True)
    assert au.duration(raw) > 9.0
    dur, peak = au.process(raw, tmp_path / "out.wav")
    assert 6.0 < dur < 7.0          # silence gone, small tail pad added
    assert -20 < peak < -5          # normalised into range


def test_silence_is_flagged(tmp_path):
    sil = tmp_path / "s.wav"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                    "-i", "anullsrc=r=48000:cl=mono", "-t", "3", str(sil)], check=True)
    dur, peak = au.process(sil, tmp_path / "o.wav")
    assert au.level_verdict(peak, dur)[0] == "bad"


# ── assemble ──

def test_plan_classifies_fit_over_under():
    rows = asm.plan([20.0, 20.0, 20.0], [20.2, 26.0, 9.0])
    assert [r["state"] for r in rows] == ["fit", "over", "under"]

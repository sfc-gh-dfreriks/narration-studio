"""App smoke tests via Streamlit AppTest.

Covers the two screens the operator actually lands on. The value here is not the
assertions so much as proving app.py imports and runs both paths at all — an
unguarded None or a bad st.* call raises on run() and is invisible until someone
opens the browser.
"""
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from streamlit.testing.v1 import AppTest    # noqa: E402

from lib import profile as pf               # noqa: E402

APP = str(ROOT / "app.py")


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """A tiny synthetic video with an audio track."""
    d = tmp_path_factory.mktemp("media")
    out = d / "clip.mp4"
    subprocess.run([
        "ffmpeg", "-y", "-v", "error",
        "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15:duration=30",
        "-f", "lavfi", "-i", "sine=frequency=220:duration=30",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-shortest", str(out)], check=True)
    return out


TRANSCRIPT = """SCENE 1 — OPENING
[VISUAL: title]

Opening narration for the first scene of this test.

SCENE 2 — MIDDLE
[VISUAL: chart]

Middle narration, somewhat longer than the opening so the two sections differ.

SCENE 3 — CLOSE
[VISUAL: summary]

Closing narration to finish.
"""


def _seed(at, clip, scenes=3):
    at.session_state["video"] = str(clip)
    at.session_state["script_text"] = TRANSCRIPT
    at.session_state["scenes"] = pf.scenes_even(30.0, scenes)
    at.session_state["strategy"] = "markers"
    return at


def test_setup_screen_runs_clean():
    at = AppTest.from_file(APP, default_timeout=90).run()
    assert not at.exception
    assert any("Narration Studio" in t.value for t in at.title)
    # AppTest does not expose file_uploader, so the gate button stands in for the
    # upload form being present and correctly disabled with no video chosen.
    gate = [b for b in at.button if "Profile video" in b.label]
    assert gate and gate[0].disabled


def test_main_screen_runs_clean(clip):
    at = _seed(AppTest.from_file(APP, default_timeout=120), clip).run()
    assert not at.exception
    labels = [t.label for t in at.tabs]
    assert any("Scenes" in x for x in labels)
    assert any("Record" in x for x in labels)
    assert any("Assemble" in x for x in labels)
    assert any("Publish" in x for x in labels)


def test_transcript_sections_align_to_scenes(clip):
    at = _seed(AppTest.from_file(APP, default_timeout=120), clip).run()
    assert not at.exception
    assert len(at.session_state["sections"]) == 3
    assert at.session_state["sections"][0].title == "OPENING"


def test_single_scene_warns_about_dissolves(clip):
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["video"] = str(clip)
    at.session_state["script_text"] = TRANSCRIPT
    at.session_state["scenes"] = pf.scenes_even(30.0, 1)
    at.session_state["strategy"] = "cuts"
    at.run()
    assert not at.exception
    # One detected scene is far more often failed detection than a truly
    # single-shot video, so the app must say so rather than proceed silently.
    assert any("dissolve" in w.value.lower() for w in at.warning)


def test_assemble_warns_when_takes_missing(clip):
    at = _seed(AppTest.from_file(APP, default_timeout=120), clip).run()
    assert not at.exception
    assert any("not recorded" in w.value.lower() for w in at.warning)


def test_render_button_disabled_without_takes(clip):
    at = _seed(AppTest.from_file(APP, default_timeout=120), clip).run()
    assert not at.exception
    btn = [b for b in at.button if "Render" in b.label]
    assert btn and btn[0].disabled


def test_no_transcript_still_runs(clip):
    at = AppTest.from_file(APP, default_timeout=120)
    at.session_state["video"] = str(clip)
    at.session_state["script_text"] = ""
    at.session_state["scenes"] = pf.scenes_even(30.0, 3)
    at.run()
    assert not at.exception
    assert at.session_state["sections"] == []


def test_split_button_increases_scene_count(clip):
    at = _seed(AppTest.from_file(APP, default_timeout=120), clip).run()
    before = len(at.session_state["scenes"])
    split = [b for b in at.button if b.key and b.key.startswith("sp")]
    assert split
    split[0].click().run()
    assert not at.exception
    assert len(at.session_state["scenes"]) == before + 1

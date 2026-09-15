"""Capture processing for narration takes.

Every take goes through the same chain regardless of where it came from — browser
mic, phone recording, a studio mic via the terminal — so scenes recorded minutes
apart at different distances still sit at one consistent level. Inconsistent
levels between segments is the single thing that makes self-recorded narration
sound amateur, and it is not fixable later without re-normalising everything.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# EBU R128 broadcast target. -16 LUFS is the streaming convention; -1.5 dBTP
# leaves headroom so lossy encoding cannot clip.
LUFS = -16
TRUE_PEAK = -1.5
LRA = 11

TRIM_THRESHOLD = "-45dB"
TRIM_DURATION = 0.1
TAIL_PAD = 0.30

AUDIO_EXT = (".wav", ".aiff", ".aif", ".m4a", ".mp3", ".caf", ".mp4", ".mov",
             ".webm", ".ogg", ".flac")


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def duration(path: Path) -> float:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "json", str(path)])
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def peak_db(path: Path) -> float:
    r = _run(["ffmpeg", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"])
    m = re.search(r"max_volume:\s*(-?[\d.]+) dB", r.stderr)
    return float(m.group(1)) if m else -99.0


def _filter_chain() -> str:
    """Trim leading and trailing silence, then normalise loudness.

    Trailing silence needs the areverse sandwich: silenceremove only trims from
    the start, so the signal is reversed, trimmed again, and reversed back. A
    short pad is then re-added so scenes do not butt together abruptly.
    """
    trim = (f"silenceremove=start_periods=1:start_duration={TRIM_DURATION}:"
            f"start_threshold={TRIM_THRESHOLD}")
    return (f"{trim},areverse,{trim},areverse,"
            f"loudnorm=I={LUFS}:TP={TRUE_PEAK}:LRA={LRA},"
            f"apad=pad_dur={TAIL_PAD}")


def process(raw: bytes | Path, dest: Path) -> tuple[float, float]:
    """Normalise a take to dest. Returns (duration, peak dB)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(raw, Path):
        src = raw
        tmp = None
    else:
        tmp = dest.parent / "_incoming"
        tmp.write_bytes(raw)
        src = tmp
    _run(["ffmpeg", "-y", "-i", str(src), "-af", _filter_chain(),
          "-ar", "48000", "-ac", "1", str(dest)])
    if tmp:
        tmp.unlink(missing_ok=True)
    return duration(dest), peak_db(dest)


def level_verdict(peak: float, dur: float) -> tuple[str, str]:
    """(status, message) for a captured take. status in ok|warn|bad."""
    if dur < 0.6:
        return "bad", ("Nothing captured — the browser or terminal has no "
                       "microphone permission for this origin.")
    if peak < -30:
        return "bad", "Almost silent. Check the right input device is selected."
    if peak > -1.0:
        return "warn", "Very close to clipping — lower the input gain."
    if peak < -14:
        return "warn", "A little quiet, but usable."
    return "ok", "Level is good."


def concat(parts: list[Path], dest: Path) -> float:
    """Join takes in order. Returns total duration."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    lst = dest.parent / "_concat.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts))
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
          "-ar", "48000", "-ac", "1", str(dest)])
    lst.unlink(missing_ok=True)
    return duration(dest)


def split_by_weights(src: Path, weights: list[float], out_dir: Path) -> list[Path]:
    """Cut a single continuous recording into per-section files.

    Split proportionally to section word count rather than on a fixed clock, so a
    boundary lands near a sentence end instead of mid-phrase. Only used when the
    operator supplies one file for the whole narration.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    total = duration(src)
    tw = sum(weights) or 1.0
    paths, acc = [], 0.0
    for i, w in enumerate(weights, 1):
        span = total * w / tw
        dest = out_dir / f"section{i}.wav"
        _run(["ffmpeg", "-y", "-ss", f"{acc:.3f}", "-t", f"{span:.3f}", "-i", str(src),
              "-ar", "48000", "-ac", "1", str(dest)])
        paths.append(dest)
        acc += span
    return paths

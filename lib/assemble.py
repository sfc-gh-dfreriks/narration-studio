"""Mux narration onto the source video.

The hard requirement is that picture and narration stay aligned. A scene's
narration is almost never exactly as long as the scene it belongs to, and the two
ways of absorbing that difference are fundamentally different edits:

  fit_audio    keep the original cut, pad or trim narration per scene. Picture
               timing is untouched, so anything cut to music or to an on-screen
               action still lines up. Narration that overruns is clipped.
  fit_video    let narration drive, stretching or trimming each scene's picture
               to match. Nothing is ever cut off mid-sentence, at the cost of
               altering the original pacing.

fit_audio is the default because most source footage is a screen recording where
pacing is incidental, and silently truncating someone's last sentence is a worse
failure than a slightly early cut.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import audio as au

ORIGINAL_REPLACE = "replace"
ORIGINAL_DUCK = "duck"
ORIGINAL_KEEP = "keep"

DUCK_LEVEL = 0.12          # original audio gain when ducked under narration


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def plan(scene_durs: list[float], take_durs: list[float]) -> list[dict]:
    """Per-scene comparison of narration length against scene length."""
    rows = []
    for i, (sd, td) in enumerate(zip(scene_durs, take_durs), 1):
        delta = td - sd
        rows.append({
            "scene": i, "scene_seconds": round(sd, 2), "take_seconds": round(td, 2),
            "delta": round(delta, 2),
            "state": "over" if delta > 0.75 else "under" if delta < -0.75 else "fit",
        })
    return rows


def _pad_or_trim(src: Path, target: float, dest: Path) -> Path:
    """Force a take to exactly `target` seconds."""
    _run(["ffmpeg", "-y", "-i", str(src),
          "-af", f"apad=whole_dur={target:.3f}",
          "-t", f"{target:.3f}", "-ar", "48000", "-ac", "1", str(dest)])
    return dest


def build(video: Path, takes: list[Path], scenes: list, dest: Path,
          original: str = ORIGINAL_REPLACE, mode: str = "fit_audio",
          work: Path | None = None) -> dict:
    """Render the narrated video. Returns a report dict."""
    work = work or dest.parent / "_work"
    work.mkdir(parents=True, exist_ok=True)

    scene_durs = [s.duration for s in scenes]
    take_durs = [au.duration(t) for t in takes]
    rows = plan(scene_durs, take_durs)

    if mode == "fit_audio":
        fitted = [_pad_or_trim(t, sd, work / f"fit{i}.wav")
                  for i, (t, sd) in enumerate(zip(takes, scene_durs), 1)]
        narration = work / "narration.wav"
        au.concat(fitted, narration)
        out = _mux(video, narration, dest, original)
    else:
        out = _rebuild_timeline(video, takes, scenes, dest, original, work)

    return {
        "output": str(out),
        "duration": au.duration(out),
        "size": out.stat().st_size if out.exists() else 0,
        "rows": rows,
        "mode": mode,
        "original": original,
        "overruns": [r for r in rows if r["state"] == "over"],
    }


def _mux(video: Path, narration: Path, dest: Path, original: str) -> Path:
    """Attach a full-length narration track to the untouched picture."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    src_has_audio = _has_audio(video)

    if original == ORIGINAL_REPLACE or not src_has_audio:
        cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(narration),
               "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
               "-c:a", "aac", "-b:a", "192k", "-shortest", str(dest)]
    else:
        gain = DUCK_LEVEL if original == ORIGINAL_DUCK else 1.0
        cmd = ["ffmpeg", "-y", "-i", str(video), "-i", str(narration),
               "-filter_complex",
               f"[0:a]volume={gain}[bg];[bg][1:a]amix=inputs=2:duration=longest:"
               f"dropout_transition=0:normalize=0[a]",
               "-map", "0:v:0", "-map", "[a]", "-c:v", "copy",
               "-c:a", "aac", "-b:a", "192k", "-shortest", str(dest)]
    _run(cmd)
    return dest


def _rebuild_timeline(video: Path, takes: list[Path], scenes: list, dest: Path,
                      original: str, work: Path) -> Path:
    """Restretch each scene's picture to its narration, then concatenate.

    Used only for fit_video. Each segment is re-encoded because setpts changes
    presentation timestamps, so stream copy is not available.
    """
    segs = []
    for i, (sc, take) in enumerate(zip(scenes, takes), 1):
        td = au.duration(take)
        if td <= 0.05:
            continue
        factor = td / sc.duration if sc.duration > 0 else 1.0
        seg = work / f"seg{i}.mp4"
        cmd = ["ffmpeg", "-y", "-ss", f"{sc.start:.3f}", "-t", f"{sc.duration:.3f}",
               "-i", str(video), "-i", str(take)]
        if original == ORIGINAL_REPLACE or not _has_audio(video):
            cmd += ["-filter_complex",
                    f"[0:v]setpts={factor:.6f}*PTS[v]",
                    "-map", "[v]", "-map", "1:a:0"]
        else:
            gain = DUCK_LEVEL if original == ORIGINAL_DUCK else 1.0
            cmd += ["-filter_complex",
                    f"[0:v]setpts={factor:.6f}*PTS[v];"
                    f"[0:a]atempo=1.0,volume={gain}[bg];"
                    f"[bg][1:a]amix=inputs=2:duration=longest:normalize=0[a]",
                    "-map", "[v]", "-map", "[a]"]
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "20",
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                "-t", f"{td:.3f}", str(seg)]
        _run(cmd)
        if seg.exists():
            segs.append(seg)

    lst = work / "segs.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in segs))
    _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
          "-c", "copy", str(dest)])
    return dest


def _has_audio(video: Path) -> bool:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "a",
              "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(video)])
    return "audio" in r.stdout

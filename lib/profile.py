"""Scene profiling for a source video.

Three strategies, because none is reliable alone:

  cuts       ffmpeg scene-change detection. Accurate on hard cuts, and blind to
             dissolves — a crossfaded slide deck returns zero cuts at every
             threshold, because a gradual dissolve never spikes the frame delta.
  markers    boundaries implied by the transcript (SCENE n, ##, ---). Exact when
             the transcript is structured, useless when it is prose.
  even       N equal segments. Always works, never insightful.

The app proposes one and lets the operator override, rather than presenting any
of them as authoritative.
"""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

MIN_SCENE_SECONDS = 4.0        # merge anything shorter into its predecessor


@dataclass
class Scene:
    index: int
    start: float
    end: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["duration"] = self.duration
        return d


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def probe_duration(video: Path) -> float:
    r = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
              "-of", "json", str(video)])
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return 0.0


def probe_video(video: Path) -> dict:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height,r_frame_rate,codec_name",
              "-show_entries", "format=duration,size", "-of", "json", str(video)])
    try:
        j = json.loads(r.stdout)
        st = (j.get("streams") or [{}])[0]
        fm = j.get("format", {})
        num, _, den = (st.get("r_frame_rate") or "0/1").partition("/")
        fps = round(float(num) / float(den or 1), 2) if den else 0.0
        return {
            "width": st.get("width"), "height": st.get("height"),
            "codec": st.get("codec_name"), "fps": fps,
            "duration": float(fm.get("duration", 0) or 0),
            "size": int(fm.get("size", 0) or 0),
            "has_audio": has_audio(video),
        }
    except Exception:
        return {}


def has_audio(video: Path) -> bool:
    r = _run(["ffprobe", "-v", "error", "-select_streams", "a",
              "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(video)])
    return "audio" in r.stdout


def detect_cuts(video: Path, threshold: float = 0.15) -> list[float]:
    """Timestamps of detected scene changes, in seconds.

    Uses metadata=print rather than showinfo: showinfo writes at ffmpeg's info
    log level, so any caller that quietens ffmpeg silently gets zero cuts and
    concludes the video has none. metadata=print writes to the chosen file
    regardless of log level.
    """
    r = _run(["ffmpeg", "-i", str(video),
              "-vf", f"select='gt(scene,{threshold})',metadata=print:file=-",
              "-an", "-f", "null", "-"])
    return sorted({round(float(m), 3)
                   for m in re.findall(r"pts_time:([0-9.]+)", r.stdout + r.stderr)})


def scenes_from_cuts(video: Path, threshold: float = 0.15,
                     total: float | None = None) -> list[Scene]:
    total = total if total is not None else probe_duration(video)
    bounds = [0.0] + [t for t in detect_cuts(video, threshold) if 0 < t < total] + [total]
    return _merge_short(_bounds_to_scenes(bounds), total)


def scenes_even(total: float, n: int) -> list[Scene]:
    n = max(1, n)
    step = total / n
    return _bounds_to_scenes([round(i * step, 3) for i in range(n)] + [total])


def scenes_from_weights(total: float, weights: list[float]) -> list[Scene]:
    """Proportional split — used to lay transcript sections onto a video."""
    tw = sum(weights) or 1.0
    bounds, acc = [0.0], 0.0
    for w in weights[:-1]:
        acc += total * w / tw
        bounds.append(round(acc, 3))
    bounds.append(total)
    return _bounds_to_scenes(bounds)


def _bounds_to_scenes(bounds: list[float]) -> list[Scene]:
    bounds = sorted(set(round(b, 3) for b in bounds))
    return [Scene(i + 1, a, b) for i, (a, b) in enumerate(zip(bounds, bounds[1:]))]


def _merge_short(scenes: list[Scene], total: float) -> list[Scene]:
    """Fold sub-threshold scenes into the previous one.

    Cut detection on UI footage fires on things like a modal opening, producing
    one-second fragments that are useless to narrate over. Merging keeps the
    proposal usable without lowering the threshold and losing real boundaries.
    """
    if not scenes:
        return [Scene(1, 0.0, total)]
    kept = [scenes[0]]
    for sc in scenes[1:]:
        if sc.duration < MIN_SCENE_SECONDS:
            kept[-1] = Scene(kept[-1].index, kept[-1].start, sc.end)
        else:
            kept.append(sc)
    if kept[-1].duration < MIN_SCENE_SECONDS and len(kept) > 1:
        last = kept.pop()
        kept[-1] = Scene(kept[-1].index, kept[-1].start, last.end)
    return [Scene(i + 1, s.start, s.end) for i, s in enumerate(kept)]


def thumbnail(video: Path, at: float, dest: Path, width: int = 480) -> Path | None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    r = _run(["ffmpeg", "-y", "-ss", f"{max(0.0, at):.3f}", "-i", str(video),
              "-frames:v", "1", "-vf", f"scale={width}:-2", str(dest)])
    return dest if dest.exists() and r.returncode == 0 else None


def split_at(scenes: list[Scene], t: float) -> list[Scene]:
    """Insert a boundary at t."""
    bounds = sorted({s.start for s in scenes} | {scenes[-1].end, round(t, 3)})
    return _bounds_to_scenes(bounds)


def remove_boundary(scenes: list[Scene], index: int) -> list[Scene]:
    """Drop the boundary that starts scene `index`, merging it backwards."""
    if index <= 1 or index > len(scenes):
        return scenes
    bounds = [s.start for s in scenes] + [scenes[-1].end]
    del bounds[index - 1]
    return _bounds_to_scenes(bounds)

"""Narration Studio — record narration over any video, scene by scene.

Upload a video and a transcript, let the video be profiled into scenes, record or
upload a take per scene, then render the narrated result.

Run:  streamlit run app.py
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path

import streamlit as st

from lib import assemble as asm
from lib import audio as au
from lib import profile as pf
from lib import publish as pub
from lib import transcript as tr

ROOT = Path(__file__).parent
WORK = ROOT / "work"
UPLOADS = WORK / "uploads"
TAKES = WORK / "takes"
THUMBS = WORK / "thumbs"
OUTPUT = WORK / "output"
STATE_FILE = WORK / "session.json"

for d in (UPLOADS, TAKES, THUMBS, OUTPUT):
    d.mkdir(parents=True, exist_ok=True)

st.set_page_config(page_title="Narration Studio", page_icon="🎙", layout="wide")

# Colours are set explicitly rather than inherited. Streamlit's theme only reaches
# its own widgets, so any custom panel left to inherit renders light-on-light for
# anyone whose OS is in light mode.
INK, DIM, EDGE = "#E8EDF2", "#A9B7C2", "#2A3441"
PANEL, PANEL2 = "#161B22", "#1C232C"
GO, STOP, WARN = "#2EA043", "#F85149", "#D29922"

st.markdown(f"""<style>
  .stApp {{ background:#0D1117; }}
  section[data-testid="stSidebar"] {{ background:{PANEL}; }}
  h1,h2,h3,h4,h5,h6,p,li,label,span,div[data-testid="stMarkdownContainer"] {{ color:{INK}; }}
  .ns-card {{ background:{PANEL}; border:1px solid {EDGE}; border-radius:10px;
              padding:14px 16px; margin-bottom:12px; }}
  .ns-card h4 {{ margin:0 0 6px 0; font-size:15px; letter-spacing:.02em; }}
  .ns-meta {{ color:{DIM}; font-size:12px; font-family:ui-monospace,Menlo,monospace; }}
  .ns-script {{ background:{PANEL2}; border-left:3px solid #3B82F6; border-radius:6px;
                padding:16px 18px; color:{INK}; font-size:19px; line-height:1.72; }}
  .ns-script p {{ margin:0 0 12px 0; }}
  .ns-cue {{ color:{DIM}; font-size:12px; font-style:italic; margin-bottom:10px; }}
  .ns-pill {{ display:inline-block; padding:2px 9px; border-radius:11px;
              font-size:11px; font-weight:600; margin-right:6px; }}
  .ns-ok   {{ background:rgba(46,160,67,.16);  color:{GO};   border:1px solid {GO}; }}
  .ns-bad  {{ background:rgba(248,81,73,.16);  color:{STOP}; border:1px solid {STOP}; }}
  .ns-warn {{ background:rgba(210,153,34,.16); color:{WARN}; border:1px solid {WARN}; }}
  .ns-todo {{ background:rgba(169,183,194,.10);color:{DIM};  border:1px solid {EDGE}; }}
  /* Mic ring: green at rest, red while recording. The record and stop states are
     the same DOM node with a different aria-label, so the label drives the ring. */
  div[data-testid="stAudioInput"] button[aria-label="Record"],
  div[class*="stAudioInputActionButton"] button[aria-label="Record"] {{
      border:3px solid {GO} !important; border-radius:50% !important;
      box-shadow:0 0 0 4px rgba(46,160,67,.20) !important; }}
  div[data-testid="stAudioInput"] button[aria-label="Stop recording"],
  div[class*="stAudioInputActionButton"] button[aria-label="Stop recording"] {{
      border:3px solid {STOP} !important; border-radius:50% !important;
      box-shadow:0 0 0 4px rgba(248,81,73,.22) !important; }}
  div[data-testid="stAudioInput"] svg {{ fill:{INK} !important; color:{INK} !important; }}
  div[class*="stAudioInputWaveformTimeCode"] {{ color:{INK} !important;
      font-family:ui-monospace,Menlo,monospace !important; }}
</style>""", unsafe_allow_html=True)


# ─────────────────────────── state ───────────────────────────

def ss(key, default):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


ss("video", None)
ss("script_text", "")
ss("scenes", [])
ss("sections", [])
ss("structured", False)
ss("strategy", "cuts")
ss("threshold", 0.15)
ss("takes", {})          # index -> {"path","dur","peak","fp","at"}
ss("report", None)
ss("queued_edit", None)


def video_path() -> Path | None:
    v = st.session_state.video
    return Path(v) if v else None


def save_state() -> None:
    STATE_FILE.write_text(json.dumps({
        "video": st.session_state.video,
        "script_text": st.session_state.script_text,
        "scenes": [s.as_dict() for s in st.session_state.scenes],
        "takes": {str(k): v for k, v in st.session_state.takes.items()},
        "strategy": st.session_state.strategy,
        "threshold": st.session_state.threshold,
    }, indent=2))


def load_state() -> bool:
    if not STATE_FILE.exists():
        return False
    try:
        d = json.loads(STATE_FILE.read_text())
    except Exception:
        return False
    if not d.get("video") or not Path(d["video"]).exists():
        return False
    st.session_state.video = d["video"]
    st.session_state.script_text = d.get("script_text", "")
    st.session_state.scenes = [pf.Scene(s["index"], s["start"], s["end"])
                               for s in d.get("scenes", [])]
    st.session_state.takes = {int(k): v for k, v in (d.get("takes") or {}).items()
                              if Path(v.get("path", "")).exists()}
    st.session_state.strategy = d.get("strategy", "cuts")
    st.session_state.threshold = d.get("threshold", 0.15)
    _resync_sections()
    return True


def _resync_sections() -> None:
    """Align the transcript to the current scene count."""
    txt = st.session_state.script_text
    if not txt.strip():
        st.session_state.sections, st.session_state.structured = [], False
        return
    n = len(st.session_state.scenes) or 1
    secs, structured = tr.parse(txt)
    st.session_state.structured = structured
    st.session_state.sections = secs if structured else tr.fit_to_count(txt, n)


def fmt(sec: float) -> str:
    sec = max(0, int(round(sec)))
    return f"{sec // 60}:{sec % 60:02d}"


# ─────────────────────────── profiling ───────────────────────────

def run_profile(strategy: str, threshold: float, n: int) -> None:
    v = video_path()
    if not v:
        return
    total = pf.probe_duration(v)
    if strategy == "cuts":
        scenes = pf.scenes_from_cuts(v, threshold, total)
    elif strategy == "markers" and st.session_state.script_text.strip():
        secs, _ = tr.parse(st.session_state.script_text)
        scenes = pf.scenes_from_weights(total, tr.weights(secs)) if secs \
            else pf.scenes_even(total, n)
    else:
        scenes = pf.scenes_even(total, n)
    st.session_state.scenes = scenes
    _resync_sections()
    save_state()


def build_thumbs() -> None:
    v = video_path()
    if not v:
        return
    for s in st.session_state.scenes:
        dest = THUMBS / f"s{s.index}_{int(s.start)}.jpg"
        if not dest.exists():
            pf.thumbnail(v, s.start + min(1.5, s.duration / 2), dest)


# ─────────────────────────── sidebar ───────────────────────────

with st.sidebar:
    st.markdown("### 🎙 Narration Studio")
    v = video_path()

    if v:
        info = pf.probe_video(v)
        st.markdown(
            f"<div class='ns-card'><h4>{v.name[:34]}</h4><div class='ns-meta'>"
            f"{info.get('width')}×{info.get('height')} · {info.get('fps')} fps<br>"
            f"{fmt(info.get('duration', 0))} · {info.get('size', 0)/1e6:.1f} MB<br>"
            f"source audio: {'yes' if info.get('has_audio') else 'none'}"
            f"</div></div>", unsafe_allow_html=True)

        done = len(st.session_state.takes)
        total_n = len(st.session_state.scenes)
        st.progress(done / total_n if total_n else 0.0,
                    text=f"{done} of {total_n} scenes recorded")

        st.divider()
        st.caption("SCENE BOUNDARIES")
        strat = st.radio("Strategy", ["cuts", "markers", "even"],
                         index=["cuts", "markers", "even"].index(st.session_state.strategy),
                         format_func=lambda x: {"cuts": "Detect scene cuts",
                                                "markers": "From transcript",
                                                "even": "Even split"}[x],
                         label_visibility="collapsed")
        th = st.session_state.threshold
        n = len(st.session_state.scenes) or 6
        if strat == "cuts":
            th = st.slider("Sensitivity", 0.04, 0.50, float(th), 0.01,
                           help="Lower finds more cuts. Dissolves and crossfades are "
                                "invisible to detection at any value.")
        elif strat == "even":
            n = st.number_input("Segments", 1, 40, int(n))

        if st.button("Re-profile video", width='stretch'):
            st.session_state.strategy, st.session_state.threshold = strat, th
            run_profile(strat, th, int(n))
            st.rerun()

        st.divider()
        if st.button("Start over", width='stretch'):
            for k in ("video", "script_text", "scenes", "sections", "takes", "report"):
                st.session_state[k] = [] if k in ("scenes", "sections") else \
                    ({} if k == "takes" else (None if k in ("video", "report") else ""))
            STATE_FILE.unlink(missing_ok=True)
            shutil.rmtree(TAKES, ignore_errors=True)
            TAKES.mkdir(parents=True, exist_ok=True)
            st.rerun()
    else:
        st.caption("Upload a video to begin.")

    st.divider()
    st.caption(f"git · {pub.status(ROOT)}")


# ─────────────────────────── setup screen ───────────────────────────

if not video_path():
    st.title("Narration Studio")
    st.caption("Record narration over any video, scene by scene.")

    if STATE_FILE.exists() and st.button("↩︎ Resume previous session"):
        if load_state():
            st.rerun()

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("#### 1 · Video")
        up = st.file_uploader("Video file", type=["mp4", "mov", "m4v", "webm", "mkv"],
                              label_visibility="collapsed")
    with c2:
        st.markdown("#### 2 · Transcript")
        tup = st.file_uploader("Transcript", type=["txt", "md"],
                               label_visibility="collapsed")
        pasted = st.text_area("or paste it", height=150,
                              placeholder="Paste narration text…\n\nSCENE markers, "
                                          "## headings or plain paragraphs all work.")

    st.caption("Scene cuts are detected automatically on load. Crossfades and "
               "dissolves cannot be detected — use the transcript or an even split "
               "for that footage.")

    if st.button("Profile video →", type="primary", disabled=up is None):
        dest = UPLOADS / up.name
        dest.write_bytes(up.getbuffer())
        st.session_state.video = str(dest)
        text = ""
        if tup:
            text = tup.getvalue().decode("utf-8", "replace")
        elif pasted.strip():
            text = pasted
        st.session_state.script_text = text
        strategy = "markers" if (text and tr.has_markers(text)) else "cuts"
        st.session_state.strategy = strategy
        with st.spinner("Profiling scenes…"):
            run_profile(strategy, st.session_state.threshold, 6)
            build_thumbs()
        st.rerun()
    st.stop()


# ─────────────────────────── main ───────────────────────────

# sections is derived from script_text and the scene count, so it is rebuilt
# whenever it has drifted rather than only at upload time. Without this, any route
# into the app that sets scenes without going through run_profile — a resumed
# session, an externally edited state file — renders an empty teleprompter.
if st.session_state.script_text.strip() and not st.session_state.sections:
    _resync_sections()

scenes = st.session_state.scenes
sections = st.session_state.sections
takes = st.session_state.takes

st.title("Narration Studio")
detected = {"cuts": "detected scene cuts", "markers": "transcript sections",
            "even": "even split"}[st.session_state.strategy]
st.caption(f"{len(scenes)} scenes from {detected} · "
           f"{sum(s.words for s in sections)} words of narration")

if st.session_state.strategy == "cuts" and len(scenes) == 1:
    st.warning("Only one scene was detected. If the video does have distinct "
               "sections, they are joined by dissolves rather than hard cuts — "
               "detection cannot see those. Switch to the transcript or an even "
               "split in the sidebar.")

tabs = st.tabs(["① Scenes", "② Record", "③ Assemble", "④ Publish"])


# ── scenes ──
with tabs[0]:
    st.markdown("#### Review boundaries")
    st.caption("Each thumbnail is a frame from inside that scene. Split or merge "
               "until the boundaries match what you want to narrate over.")
    build_thumbs()

    for s in scenes:
        c = st.columns([1.1, 2.4, 1.0])
        with c[0]:
            hits = sorted(THUMBS.glob(f"s{s.index}_*.jpg"))
            if hits:
                st.image(str(hits[-1]), width='stretch')
        with c[1]:
            sec = next((x for x in sections if x.index == s.index), None)
            st.markdown(
                f"<div class='ns-card'><h4>Scene {s.index}"
                f"{' — ' + sec.title if sec else ''}</h4>"
                f"<div class='ns-meta'>{fmt(s.start)} → {fmt(s.end)} · "
                f"{s.duration:.1f}s scene"
                + (f" · {sec.words} words ≈ {sec.target:.0f}s narration"
                   if sec else "")
                + "</div></div>", unsafe_allow_html=True)
            if sec and sec.target > s.duration * 1.35:
                st.caption(f"⚠︎ narration runs ~{sec.target - s.duration:.0f}s longer "
                           f"than the scene")
        with c[2]:
            mid = s.start + s.duration / 2
            if st.button(f"Split at {fmt(mid)}", key=f"sp{s.index}",
                         width='stretch'):
                st.session_state.scenes = pf.split_at(scenes, mid)
                st.session_state.takes = {}
                _resync_sections(); save_state(); st.rerun()
            if s.index > 1 and st.button("Merge up", key=f"mg{s.index}",
                                         width='stretch'):
                st.session_state.scenes = pf.remove_boundary(scenes, s.index)
                st.session_state.takes = {}
                _resync_sections(); save_state(); st.rerun()

    if st.session_state.structured:
        st.info("This transcript has explicit section markers, so its own structure "
                "is used rather than the detected count.")


# ── record ──
with tabs[1]:
    if not sections:
        st.warning("No transcript loaded. Add one in the sidebar to get a teleprompter.")
    labels = [f"{'✓' if i.index in takes else '○'} {i.index}. {i.title[:22]}"
              for i in sections] or [f"○ Scene {s.index}" for s in scenes]
    pick = st.radio("Scene", range(len(labels)), format_func=lambda i: labels[i],
                    horizontal=True, label_visibility="collapsed")

    idx = pick + 1
    sec = next((x for x in sections if x.index == idx), None)
    sc = next((x for x in scenes if x.index == idx), None)
    take = takes.get(idx)

    left, right = st.columns([2.5, 1.2])

    with left:
        if sec:
            st.markdown(f"#### Scene {idx} — {sec.title}")
            if sec.cue:
                st.markdown(f"<div class='ns-cue'>{sec.cue}</div>",
                            unsafe_allow_html=True)
            body = "".join(f"<p>{p}</p>" for p in sec.paras)
            st.markdown(f"<div class='ns-script'>{body}</div>", unsafe_allow_html=True)
            st.caption(f"{sec.words} words · target ≈ {sec.target:.0f}s"
                       + (f" · scene is {sc.duration:.0f}s" if sc else ""))

            with st.expander("Edit this section"):
                # Queued-value pattern: session_state for a widget key cannot be
                # written after the widget exists, so an edit is parked and applied
                # on the next run before the text_area is created.
                q = st.session_state.queued_edit
                if q and q[0] == idx:
                    st.session_state[f"ed{idx}"] = q[1]
                    st.session_state.queued_edit = None
                draft = st.text_area("Body", value="\n\n".join(sec.paras),
                                     height=200, key=f"ed{idx}")
                if st.button("Save section", key=f"sv{idx}"):
                    new = tr.replace_section(sections, idx, draft)
                    st.session_state.sections = new
                    st.session_state.script_text = tr.render(new)
                    st.session_state.queued_edit = (idx, draft)
                    save_state()
                    st.rerun()
        elif sc:
            st.markdown(f"#### Scene {idx}")
            st.caption(f"{fmt(sc.start)} → {fmt(sc.end)} · no transcript section")

        if sc:
            st.video(str(video_path()), start_time=int(sc.start))

    with right:
        st.markdown("#### Capture")
        if take:
            stale = sec and take.get("fp") and take["fp"] != sec.fingerprint
            cls = "ns-warn" if stale else "ns-ok"
            msg = "script changed since take" if stale else "recorded"
            st.markdown(f"<span class='ns-pill {cls}'>{msg}</span>"
                        f"<div class='ns-meta' style='margin-top:6px'>"
                        f"{take['dur']:.1f}s · peak {take['peak']:.1f} dB</div>",
                        unsafe_allow_html=True)
            st.audio(take["path"])
        else:
            st.markdown("<span class='ns-pill ns-todo'>not recorded</span>",
                        unsafe_allow_html=True)

        st.write("")
        mic, upl = st.tabs(["Microphone", "Upload"])

        def store(raw, idx=idx, sec=sec):
            dest = TAKES / f"scene{idx}.wav"
            dur, peak = au.process(raw, dest)
            status, note = au.level_verdict(peak, dur)
            st.session_state.takes[idx] = {
                "path": str(dest), "dur": dur, "peak": peak,
                "fp": sec.fingerprint if sec else "",
                "at": datetime.now().isoformat(timespec="seconds"),
            }
            save_state()
            (st.error if status == "bad" else st.warning if status == "warn"
             else st.success)(note)

        with mic:
            clip = st.audio_input("Record", key=f"mic{idx}",
                                  label_visibility="collapsed")
            if clip and st.button("Use this take", key=f"um{idx}",
                                  width='stretch', type="primary"):
                store(clip.getvalue())
                st.rerun()
            st.caption("Green ring records, red stops. If the mic button is "
                       "missing the browser has blocked audio for this origin — "
                       "use Upload instead.")

        with upl:
            f = st.file_uploader("Audio", type=[e.strip(".") for e in au.AUDIO_EXT],
                                 key=f"up{idx}", label_visibility="collapsed")
            if f and st.button("Use this file", key=f"uf{idx}",
                               width='stretch'):
                store(f.getvalue())
                st.rerun()
            st.caption("Any recorder works — phone, Voice Memos, a USB mic. "
                       "Levels are normalised on import.")

        if take and st.button("Discard take", key=f"dl{idx}",
                              width='stretch'):
            Path(take["path"]).unlink(missing_ok=True)
            del st.session_state.takes[idx]
            save_state(); st.rerun()

        st.divider()
        st.caption("ONE FILE FOR EVERYTHING")
        whole = st.file_uploader("Full narration", key="whole",
                                 type=[e.strip(".") for e in au.AUDIO_EXT],
                                 label_visibility="collapsed")
        if whole and st.button("Split across scenes", width='stretch'):
            src = TAKES / "_full_input"
            src.write_bytes(whole.getvalue())
            w = tr.weights(sections) if sections else [1] * len(scenes)
            with st.spinner("Splitting…"):
                parts = au.split_by_weights(src, w, TAKES)
                for i, p in enumerate(parts, 1):
                    dest = TAKES / f"scene{i}.wav"
                    dur, peak = au.process(p, dest)
                    s_i = next((x for x in sections if x.index == i), None)
                    st.session_state.takes[i] = {
                        "path": str(dest), "dur": dur, "peak": peak,
                        "fp": s_i.fingerprint if s_i else "",
                        "at": datetime.now().isoformat(timespec="seconds")}
            src.unlink(missing_ok=True)
            save_state(); st.rerun()


# ── assemble ──
with tabs[2]:
    missing = [s.index for s in scenes if s.index not in takes]
    if missing:
        st.warning(f"Not recorded yet: scenes {', '.join(map(str, missing))}")

    stale = [s.index for s in sections
             if s.index in takes and takes[s.index].get("fp")
             and takes[s.index]["fp"] != s.fingerprint]
    if stale:
        st.warning(f"Script changed after recording: scenes "
                   f"{', '.join(map(str, stale))}. Re-record or the audio will not "
                   f"match the transcript.")

    c1, c2 = st.columns(2)
    with c1:
        original = st.radio(
            "Existing audio on the video",
            [asm.ORIGINAL_REPLACE, asm.ORIGINAL_DUCK, asm.ORIGINAL_KEEP],
            format_func=lambda x: {"replace": "Replace with narration",
                                   "duck": "Duck under narration",
                                   "keep": "Keep at full level"}[x])
    with c2:
        mode = st.radio(
            "When narration and scene lengths differ",
            ["fit_audio", "fit_video"],
            format_func=lambda x: {"fit_audio": "Keep original cut (pad/trim audio)",
                                   "fit_video": "Keep all narration (restretch picture)"}[x])

    if takes:
        rows = asm.plan([s.duration for s in scenes],
                        [takes.get(s.index, {}).get("dur", 0.0) for s in scenes])
        over = [r for r in rows if r["state"] == "over"]
        st.dataframe(rows, width='stretch', hide_index=True)
        if over and mode == "fit_audio":
            st.caption(f"⚠︎ {len(over)} scene(s) have narration longer than the "
                       f"scene. With the original cut kept, the overrun is trimmed — "
                       f"choose the other option to keep every word.")

    if st.button("Render narrated video", type="primary", disabled=not takes):
        ordered = [Path(takes[s.index]["path"]) for s in scenes if s.index in takes]
        used = [s for s in scenes if s.index in takes]
        out = OUTPUT / f"{video_path().stem}_narrated.mp4"
        with st.spinner("Rendering…"):
            st.session_state.report = asm.build(
                video_path(), ordered, used, out, original=original, mode=mode)
        st.rerun()

    rep = st.session_state.report
    if rep and Path(rep["output"]).exists():
        st.success(f"Rendered · {fmt(rep['duration'])} · {rep['size']/1e6:.1f} MB")
        st.video(rep["output"])
        st.download_button("Download", Path(rep["output"]).read_bytes(),
                           file_name=Path(rep["output"]).name, mime="video/mp4")


# ── publish ──
with tabs[3]:
    st.markdown("#### Publish the tool to GitHub")
    ok, who = pub.gh_ready()
    if ok:
        st.markdown(f"<span class='ns-pill ns-ok'>gh · {who}</span>",
                    unsafe_allow_html=True)
    else:
        st.error(who)

    st.caption("Only the tool is published. Uploaded video, narration takes and "
               "rendered output are excluded, so nothing you have loaded here can "
               "reach the remote.")

    slug = st.text_input("Repository", value=f"{who if ok else 'you'}/narration-studio")
    private = st.radio("Visibility", [True, False],
                       format_func=lambda p: "Private" if p else "Public")
    msg = st.text_input("Commit message", value="Narration Studio")

    with st.expander(f"Files that will be committed ({len(pub.TRACKED)})"):
        for f in pub.TRACKED:
            mark = "✓" if (ROOT / f).exists() else "·"
            st.markdown(f"<div class='ns-meta'>{mark} {f}</div>",
                        unsafe_allow_html=True)

    if st.button("Publish", type="primary", disabled=not ok):
        with st.spinner("Publishing…"):
            res = pub.publish(ROOT, slug.strip(), private=private, message=msg)
        for line in res["log"]:
            (st.success if line.startswith("ok") else st.error)(line)
        if res["ok"]:
            st.markdown(f"**{res['url']}**")

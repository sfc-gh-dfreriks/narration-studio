# Narration Studio

Record narration over any video, scene by scene, in the browser.

Upload a video and a transcript. The video is profiled into scenes, the transcript
is laid onto them, and you record a take per scene against a teleprompter. The
result is rendered back out as a narrated video.

Built because recording narration in one pass over a long screen recording means
restarting from the top every time you fumble a sentence. Working a scene at a
time means a retake costs twenty seconds, not ten minutes.

---

## Requirements

- Python 3.10+
- `ffmpeg` and `ffprobe` on `PATH` — `brew install ffmpeg`
- `gh` CLI, only for the publish tab — `brew install gh && gh auth login`

```bash
pip install -r requirements.txt
streamlit run app.py
```

Microphone capture needs a trusted origin. `localhost` qualifies; a bare LAN IP
does not, and the mic button will be missing. Upload works everywhere.

---

## How scenes are found

Three strategies, because none is reliable on its own.

| Strategy | Mechanism | Use it when |
|---|---|---|
| **Detect scene cuts** | ffmpeg frame-delta detection | Screen recordings, anything with hard cuts |
| **From transcript** | `SCENE n`, `##`, `---` markers, split proportionally | The transcript already states the structure |
| **Even split** | N equal segments | Continuous footage with no real boundaries |

**Cut detection cannot see dissolves.** A slide deck joined by half-second
crossfades returns zero cuts at every sensitivity, because a gradual dissolve
never spikes the frame delta — that is a property of the measure, not a tuning
problem. When the app reports one scene for a video you know has ten, that is what
happened; switch to the transcript or an even split.

Detected boundaries are a proposal. Every scene can be split or merged by hand,
and each is shown with a thumbnail from inside it so you can see what you are
narrating over.

Scenes shorter than four seconds are folded into the previous one. Cut detection
on UI footage fires on things like a modal opening, and one-second fragments are
not narratable.

---

## Transcript handling

A transcript with markers is treated as authoritative — its structure is a
deliberate statement and is never silently regrouped to match a detected scene
count. Prose with no markers is split by paragraph and grouped proportionally by
word count to match the scenes.

```
SCENE 1 — OPENING
[VISUAL: title card]

Spoken narration goes here.
```

Bracketed lines are stage direction. They are shown as a cue, excluded from the
spoken word count, and preserved when the file is saved.

Sections are editable in place. Each take records a fingerprint of the words it
was recorded against, so editing a script after recording flags that scene as
stale rather than leaving you to notice the mismatch in the render.

---

## Recording

Two capture paths, because browser mic permission is the most common failure and a
tool that only supports the mic is unusable when it is denied:

- **Microphone** — record in the browser. Green ring at rest, red while recording.
- **Upload** — any file from any recorder: phone, Voice Memos, a USB mic.

There is also a single-file path: drop one continuous recording of the whole
script and it is split across scenes proportionally by word count.

Every take gets the same treatment on import — leading and trailing silence
trimmed, loudness normalised to −16 LUFS with −1.5 dBTP ceiling. Inconsistent
levels between scenes recorded minutes apart is the single thing that makes
self-recorded narration sound amateur, and it cannot be fixed afterwards without
re-normalising everything.

Takes are checked on arrival and reported as good, quiet, near-clipping or silent.
A silent take almost always means the wrong input device, not a bad performance.

---

## Assembly

Narration is rarely exactly as long as the scene it belongs to. The two ways of
absorbing that are different edits, and the choice is yours:

- **Keep the original cut** (default) — picture timing is untouched, narration is
  padded or trimmed per scene. Anything cut to music or an on-screen action stays
  aligned. Overrunning narration is clipped.
- **Keep all narration** — each scene's picture is restretched to its take.
  Nothing is ever cut off mid-sentence, at the cost of altering the pacing.

The assemble tab shows the per-scene difference before you commit, so an overrun
is visible rather than discovered in the output.

Existing audio on the source video can be replaced (default), ducked under the
narration, or kept at full level.

---

## Publishing

The publish tab pushes **the tool**, never the media. Uploaded footage, narration
takes and rendered output are gitignored, and the publisher commits from an
explicit file allowlist rather than `git add -A` — so running it on a machine
holding customer recordings cannot push them to a remote even if `.gitignore` is
later edited.

---

## Layout

```
app.py                 Streamlit UI
lib/profile.py         scene detection, split strategies, thumbnails
lib/transcript.py      parse, align, edit, save
lib/audio.py           trim, normalise, level checks, splitting
lib/assemble.py        ffmpeg mux and timeline rebuild
lib/publish.py         gh wrapper
work/                  uploads, takes, thumbnails, output — all gitignored
```

State is checkpointed to `work/session.json`, so closing the browser mid-project
does not lose recorded takes.

```bash
python3 -m pytest tests -q
```

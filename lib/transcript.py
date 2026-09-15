"""Transcript parsing, section alignment and write-back.

A transcript arrives in one of two shapes and they need different handling:

  structured   carries explicit section markers (SCENE 3, ## Heading, ---).
               The markers ARE the intended boundaries, so they are trusted over
               anything detected from the video.
  prose        no markers. Split by paragraph and laid onto the video's scenes
               proportionally by word count.

Sections are kept as a list of paragraph lists rather than one blob, so the
teleprompter can render paragraph breaks and a round-trip through save() does not
reflow the operator's structure.
"""
from __future__ import annotations

import hashlib
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

# Recognised section markers, tried in order. Deliberately narrow: matching any
# bold line or any capitalised line turns ordinary prose into 40 fake sections.
MARKER_PATTERNS = [
    r"^\s*SCENE\s+\d+.*$",          # SCENE 3 — TITLE
    r"^\s*#{1,3}\s+\S.*$",          # markdown headings
    r"^\s*-{3,}\s*$",               # --- rules
    r"^\s*={3,}\s*$",               # === rules
    r"^\s*\[\s*\d+\s*\].*$",        # [3] Title
]
_MARKER_RE = re.compile("|".join(f"(?:{p})" for p in MARKER_PATTERNS),
                        re.MULTILINE | re.IGNORECASE)

# Lines that are stage direction, not narration. Excluded from the spoken text
# but preserved in the file so the operator's cues survive a save.
CUE_RE = re.compile(r"^\s*\[(?!\s*\d+\s*\]).*\]\s*$")

WPM = 150


@dataclass
class Section:
    index: int
    title: str
    paras: list[str] = field(default_factory=list)
    cue: str = ""
    raw_marker: str = ""

    @property
    def text(self) -> str:
        return " ".join(self.paras)

    @property
    def words(self) -> int:
        return len(self.text.split())

    @property
    def target(self) -> float:
        return self.words / WPM * 60

    @property
    def fingerprint(self) -> str:
        """Whitespace-insensitive hash, so reflowing does not invalidate a take."""
        return hashlib.sha1(" ".join(self.text.split()).encode()).hexdigest()[:12]


def has_markers(text: str) -> bool:
    return bool(_MARKER_RE.search(text or ""))


def parse(text: str) -> tuple[list[Section], bool]:
    """Return (sections, structured). Never returns an empty list for non-empty input."""
    text = (text or "").replace("\r\n", "\n").strip()
    if not text:
        return [], False
    if has_markers(text):
        return _parse_structured(text), True
    return _parse_prose(text), False


def _clean_title(marker: str) -> str:
    t = re.sub(r"^\s*#{1,3}\s*", "", marker).strip()
    t = re.sub(r"^SCENE\s+\d+\s*[—–-]?\s*", "", t, flags=re.I).strip()
    t = re.sub(r"^\[\s*\d+\s*\]\s*", "", t).strip()
    t = re.sub(r"\s*\(.*?\)\s*$", "", t).strip()
    return t or "Untitled"


def _split_paras(lines: list[str]) -> tuple[list[str], str]:
    paras, cur, cue = [], [], ""
    for ln in lines:
        s = ln.strip()
        if CUE_RE.match(s):
            cue = cue or s.strip("[]").strip()
            continue
        if not s:
            if cur:
                paras.append(" ".join(cur))
                cur = []
            continue
        cur.append(s)
    if cur:
        paras.append(" ".join(cur))
    return paras, cue


def _parse_structured(text: str) -> list[Section]:
    marks = list(_MARKER_RE.finditer(text))
    out = []
    for i, m in enumerate(marks):
        body = text[m.end(): marks[i + 1].start() if i + 1 < len(marks) else len(text)]
        paras, cue = _split_paras(body.split("\n"))
        if not paras:
            continue                      # a bare rule with no narration under it
        out.append(Section(len(out) + 1, _clean_title(m.group(0)),
                           paras, cue, m.group(0).strip()))
    if not out:                            # markers matched but nothing spoken
        return _parse_prose(text)
    return out


def _parse_prose(text: str, target_sections: int | None = None) -> list[Section]:
    paras, _ = _split_paras(text.split("\n"))
    if not paras:
        paras = [text]
    if target_sections and target_sections < len(paras):
        paras = _rebalance(paras, target_sections)
    return [Section(i + 1, f"Section {i + 1}", [p]) for i, p in enumerate(paras)]


def _rebalance(paras: list[str], n: int) -> list[str]:
    """Group paragraphs into exactly n buckets of roughly equal word count.

    A plain greedy fill with a tolerance factor cannot guarantee the count — with
    nine equal paragraphs and n=4 it returns three, because each bucket overshoots
    slightly and the loop runs out of input. This reserves one paragraph per
    remaining bucket and retargets on what is left, so the requested count is
    always produced when there are enough paragraphs to make it.
    """
    n = max(1, min(n, len(paras)))
    wc = [len(p.split()) for p in paras]
    remaining = sum(wc)
    out, i = [], 0
    for b in range(n):
        buckets_left = n - b
        # never consume a paragraph another bucket still needs
        max_take = len(paras) - i - (buckets_left - 1)
        if buckets_left == 1:
            out.append(" ".join(paras[i:]))
            break
        target = remaining / buckets_left
        take, w = 1, wc[i]
        while take < max_take and w + wc[i + take] <= target * 1.15:
            w += wc[i + take]
            take += 1
        out.append(" ".join(paras[i:i + take]))
        i += take
        remaining -= w
    return out


def fit_to_count(text: str, n: int) -> list[Section]:
    """Force a transcript into exactly n sections, to match detected scenes.

    Structured transcripts are left alone — their markers are a deliberate
    statement about structure and silently regrouping them would discard it.
    """
    secs, structured = parse(text)
    if structured or not secs or n <= 0:
        return secs
    return _parse_prose(" \n\n".join(s.text for s in secs), target_sections=n)


def weights(sections: list[Section]) -> list[float]:
    return [max(1, s.words) for s in sections]


def render(sections: list[Section]) -> str:
    """Serialise back to a transcript file, preserving markers and cues."""
    out = []
    for s in sections:
        out.append(s.raw_marker or f"SCENE {s.index} — {s.title.upper()}")
        if s.cue:
            out.append(f"[{s.cue}]")
        out.append("")
        for p in s.paras:
            out.extend(textwrap.wrap(p, 78))
            out.append("")
    return "\n".join(out).rstrip() + "\n"


def save(sections: list[Section], dest: Path) -> None:
    dest.write_text(render(sections))


def replace_section(sections: list[Section], index: int, body: str) -> list[Section]:
    """Return a copy with section `index` replaced by `body` (blank-line paras)."""
    paras = [" ".join(b.split()) for b in re.split(r"\n\s*\n", body.strip()) if b.strip()]
    out = []
    for s in sections:
        if s.index == index:
            out.append(Section(s.index, s.title, paras or [""], s.cue, s.raw_marker))
        else:
            out.append(s)
    return out

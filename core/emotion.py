"""
emotion — content-driven emotion tagging for avatar acting (R2).

Free, offline, deterministic: turns a model reply into ONE of
{happy, concerned, thinking, surprised, neutral}, strips any explicit
`[emotion:…]` marker from what gets spoken/logged, and lets the avatar
(HoloAvatar) ACT it for a few seconds (brows/lids/gaze bias).

Heuristics only — honest recall, zero API, no model call.
"""
from __future__ import annotations

import re

EMOTIONS = ("happy", "concerned", "thinking", "surprised", "neutral")

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("happy", re.compile(
        r"\b(congrat|great job|awesome|amazing|well done|love it|yay|"
        r"fantastic|excellent|nicely done|celebrate|🎉|😊|😄)\b", re.I)),
    ("concerned", re.compile(
        r"\b(sorry|unfortunately|apolog|warning|careful|be cautious|"
        r"didn't work|failed|problem|issue detected|alas|⚠)\b", re.I)),
    ("thinking", re.compile(
        r"\b(hmm+|let me think|looking into|i'll check|investigat|"
        r"give me a moment|processing)\b", re.I)),
    ("surprised", re.compile(
        r"\b(whoa|wow|unexpected|i can't believe|surprising|really\?|"
        r"oh wow|astonishing)\b", re.I)),
]


def tag(text: str) -> str:
    """Emotion for a reply — explicit marker wins, else first keyword
    hit, else neutral. Never raises."""
    s = str(text or "")
    m = re.search(r"\[emotion:\s*([a-z]+)\s*\]", s, re.I)
    if m and m.group(1).lower() in EMOTIONS:
        return m.group(1).lower()
    for name, pat in _PATTERNS:
        if pat.search(s):
            return name
    return "neutral"


def strip(text: str) -> str:
    """Remove explicit emotion markers so TTS/history never speak them."""
    return re.sub(r"\[emotion:\s*[a-z]+\s*\]", "", str(text or ""),
                  flags=re.I).strip()


def tag_and_clean(text: str) -> tuple[str, str]:
    return tag(text), strip(text)

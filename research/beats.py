"""Narrative beat map from measured sentence timing.

Beat boundaries come from word timestamps; the role labels come from cue
words and position, and every beat carries the confidence and the cue that
set it. Boundaries snap to a 100-500 ms grid and the map always covers
``[0, duration_ms]`` without gaps or overlaps.
"""

from __future__ import annotations

import re

from .timing import normalize_words, split_sentences

ROLES = ("hook", "setup", "tension", "evidence", "reveal", "payoff", "cta")
_CUES: dict[str, tuple[str, ...]] = {
    "cta": (
        "follow", "subscribe", "comment", "save this", "share this", "link in bio",
        "like this", "send this", "part 2", "let me know",
    ),
    "reveal": (
        "but", "however", "actually", "turns out", "in reality", "the truth",
        "instead", "it's not", "wrong", "myth",
    ),
    "evidence": (
        "because", "study", "research", "data", "according", "percent", "scientists",
        "measured", "for example", "proof",
    ),
    "tension": (
        "problem", "mistake", "why", "nobody", "secret", "never", "what if", "but why",
        "the catch", "risk",
    ),
    "payoff": (
        "so ", "that's why", "which means", "the answer", "finally", "the result",
        "here's", "that is how", "now you know",
    ),
}


class BeatValidationError(ValueError):
    pass


def _cue_role(text: str) -> tuple[str | None, str | None]:
    lowered = f" {text.lower()} "
    for role in ("cta", "reveal", "evidence", "tension", "payoff"):
        for cue in _CUES[role]:
            if re.search(rf"(?<![a-z]){re.escape(cue.strip())}(?![a-z])", lowered):
                return role, cue.strip()
    return None, None


def _snap(ms: int, grid: int) -> int:
    return int(round(ms / grid) * grid)


def segment_beats(words, duration_ms: int, resolution_ms: int = 100) -> list[dict]:
    if not 100 <= resolution_ms <= 500:
        raise ValueError("resolution_ms must be between 100 and 500")
    sentences = split_sentences(normalize_words(words))
    if not sentences:
        return []

    labelled = []
    for i, sentence in enumerate(sentences):
        role, cue = _cue_role(sentence["text"])
        if i == 0:
            role, confidence, cue = "hook", 0.9, "first sentence"
        elif role is None:
            role, confidence = ("setup", 0.5) if i == 1 else ("evidence", 0.3)
        else:
            confidence = 0.7
        # A CTA cue early in the video is more likely a hook line ("follow along").
        if role == "cta" and i < len(sentences) - 2:
            role, confidence = "evidence", 0.3
        labelled.append({**sentence, "role": role, "confidence": confidence, "evidence": cue})

    non_cta = [b for b in labelled if b["role"] != "cta"]
    if len(non_cta) >= 3 and not any(b["role"] == "payoff" for b in non_cta):
        last = non_cta[-1]
        last.update(role="payoff", confidence=0.4, evidence="last non-CTA sentence")

    merged: list[dict] = []
    for beat in labelled:
        if merged and merged[-1]["role"] == beat["role"]:
            prev = merged[-1]
            prev["end_ms"] = beat["end_ms"]
            prev["text"] = f"{prev['text']} {beat['text']}"
            prev["confidence"] = round(min(prev["confidence"], beat["confidence"]), 2)
        else:
            merged.append(dict(beat))

    # Contiguous map: each beat runs from its first word (snapped to the grid)
    # to the next beat's start; the first starts at 0, the last ends at the
    # duration. A beat whose snapped start collides with its predecessor's is
    # folded into it.
    kept: list[dict] = []
    for i, beat in enumerate(merged):
        start = 0 if i == 0 else _snap(beat["start_ms"], resolution_ms)
        if kept and (start <= kept[-1]["start_ms"] or start >= duration_ms):
            continue
        kept.append({
            "start_ms": start,
            "role": beat["role"],
            "confidence": beat["confidence"],
            "evidence": beat["evidence"],
            "word_count": len(beat["text"].split()),
        })
    for i, beat in enumerate(kept):
        beat["end_ms"] = kept[i + 1]["start_ms"] if i + 1 < len(kept) else duration_ms
    validate_beats(kept, duration_ms)
    return kept


def validate_beats(beats: list[dict], duration_ms: int) -> None:
    previous_end = 0
    for i, beat in enumerate(beats):
        start, end = beat["start_ms"], beat["end_ms"]
        if beat["role"] not in ROLES:
            raise BeatValidationError(f"beat {i}: unknown role {beat['role']!r}")
        if not 0 <= start < end <= duration_ms:
            raise BeatValidationError(f"beat {i}: need 0 <= start < end <= duration ({start}, {end})")
        if start < previous_end:
            raise BeatValidationError(f"beat {i} overlaps the previous beat")
        previous_end = end


def abstract_template(beats: list[dict], duration_ms: int) -> dict:
    """Blank-slot structure for script writing. Carries no source wording."""
    steps = [
        {
            "role": b["role"],
            "start_pct": round(b["start_ms"] * 100 / duration_ms, 1),
            "end_pct": round(b["end_ms"] * 100 / duration_ms, 1),
        }
        for b in beats
    ]
    label = " -> ".join(
        f"{b['start_ms'] / 1000:g}-{b['end_ms'] / 1000:g}s {b['role']}" for b in beats
    )
    payoff = next((b for b in beats if b["role"] in {"payoff", "reveal"}), None)
    return {
        "sequence": [b["role"] for b in beats],
        "steps": steps,
        "label": label,
        "first_payoff_pct": round(payoff["start_ms"] * 100 / duration_ms, 1) if payoff else None,
    }

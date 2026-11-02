"""Hook archetypes and the deterministic classifier used when Jev is off.

The archetype list overlaps with the hook formulas in sergebulaev/instagram-skills
(`ig-hook-extractor`, MIT) but is written for spoken Reel openings. Input text
is untrusted data: it is only pattern-matched, never interpreted.
"""

from __future__ import annotations

import re

ARCHETYPES: dict[str, str] = {
    "question": "opens by asking the viewer a direct question",
    "contrarian": "contradicts a common belief or advice",
    "number_first": "leads with a specific number, result or list size",
    "shock_stat": "opens with a surprising statistic or fact",
    "demonstration": "immediately shows or does the thing on screen",
    "open_loop": "promises a payoff that comes later (wait for it, here's what happened)",
    "story": "first-person story or confession with a time marker",
    "how_to": "promises a method or teardown (how I / how to)",
    "relatable": "names a specific shared moment or frustration",
    "other": "none of the above",
}

_RULES: list[tuple[str, re.Pattern, float]] = [
    ("contrarian", re.compile(r"\b(stop|myth|wrong|lie|lied|not what you think|everyone thinks|actually|isn't|aren't|don't)\b"), 0.7),
    ("how_to", re.compile(r"\b(how (i|to|we)|here'?s how|step[s]? to|the way to)\b"), 0.75),
    ("story", re.compile(r"\b(i (was|did|got|tried|spent|quit|lost|made)|last (year|week|month)|yesterday|years ago)\b"), 0.65),
    ("open_loop", re.compile(r"\b(wait (for it|till|until)|watch (till|until) the end|you won'?t believe|what happened next|by the end)\b"), 0.7),
    ("demonstration", re.compile(r"\b(watch (this|me|what)|look at (this|what)|i'?m going to show|let me show)\b"), 0.65),
    ("relatable", re.compile(r"\b(pov|when you|that moment|me when|you know when)\b"), 0.6),
]
_NUMBER = re.compile(r"\b\d[\d,.]*\s*(%|percent|x|times|k|m|million|billion|years?|days?|hours?|minutes?|seconds?)?")


def classify_hook(text: str | None) -> dict:
    """Return ``{archetype, confidence, scores, method}``; ``other`` on no evidence."""
    clean = re.sub(r"\s+", " ", (text or "")).strip().lower()
    if not clean:
        return {"archetype": "other", "confidence": 0.0, "scores": {}, "method": "rules_v1", "reason": "no hook text"}

    scores: dict[str, float] = {}
    first_clause = re.split(r"[.!]", clean, maxsplit=1)[0]
    if "?" in clean[:150]:
        scores["question"] = 0.8 if first_clause.endswith("?") or "?" in first_clause else 0.6
    number = _NUMBER.search(clean[:80])
    if number:
        unit = (number.group(1) or "").strip()
        if unit in {"%", "percent", "x", "times", "million", "billion"}:
            scores["shock_stat"] = 0.7
        else:
            scores["number_first"] = 0.65 if number.start() < 15 else 0.5
    for archetype, pattern, weight in _RULES:
        if pattern.search(clean):
            scores[archetype] = max(scores.get(archetype, 0.0), weight)

    if not scores:
        return {"archetype": "other", "confidence": 0.3, "scores": {}, "method": "rules_v1"}
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    best, confidence = ranked[0]
    # Two near-equal matches mean the opening mixes formulas: lower confidence.
    if len(ranked) > 1 and ranked[1][1] >= confidence - 0.05:
        confidence -= 0.15
    return {
        "archetype": best,
        "confidence": round(confidence, 2),
        "scores": dict(ranked),
        "method": "rules_v1",
    }

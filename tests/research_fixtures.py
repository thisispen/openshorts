"""Synthetic research fixtures. Every record is marked is_synthetic and uses
made-up shortcodes: none of this describes a real creator or video."""

from __future__ import annotations

HOOKS = [
    "Stop believing this space myth.",
    "Why does the Moon look bigger near the horizon?",
    "97% of people get this wrong about lightning.",
    "How I learned to spot fake science in 3 days",
    "Wait for it, the ending changes everything.",
    "Last year I tried sleeping 4 hours a night.",
    "POV: you just found out goldfish memory is fine.",
    "Watch this glass of water freeze instantly.",
]


def synthetic_sources(n: int = 20) -> list[dict]:
    records = []
    for i in range(n):
        likes = None if i % 7 == 3 else 1000 + i * 150
        record = {
            "source_url": f"https://www.instagram.com/reel/SYNTH{i:04d}abc/?igsh=tracking{i}",
            "creator_handle": f"synthetic_creator_{i % 5}",
            "caption_excerpt": HOOKS[i % len(HOOKS)],
            "provider": "manual",
            "is_synthetic": True,
            "metrics": {
                "provider": "manual",
                "metric_scope": "public",
                "likes": likes,
                "comments": 40 + i,
                "followers": 10_000 + 2_000 * (i % 5),
                "views": None,
                "missing_reasons": {"views": "not_exposed_by_api"} | ({"likes": "hidden_by_creator"} if likes is None else {}),
            },
        }
        records.append(record)
    return records


def owned_video_words() -> list[dict]:
    """A 30 s owned explainer: hook question, setup, reveal, evidence, payoff, CTA."""
    script = [
        (0, "Why do we only use ten percent of our brain?"),
        (3200, "That line shows up in movies all the time."),
        (6400, "But it's not true at all."),
        (8600, "Brain scans show activity across almost every region during a normal day."),
        (14200, "Even sleep keeps large areas busy."),
        (17000, "So the myth survives because it sounds hopeful, not because it is real."),
        (24500, "Follow for one myth a day."),
    ]
    words = []
    for start, sentence in script:
        t = start
        for token in sentence.split():
            dur = 180 + 25 * len(token)
            words.append({"text": token, "start_ms": t, "end_ms": t + dur})
            t += dur + 60
    return words

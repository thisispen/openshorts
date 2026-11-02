"""Descriptive cohort statistics with explicit sample sizes and uncertainty.

NULL metrics are excluded from each statistic (never counted as zero), every
figure reports its own n, and synthetic records are excluded unless asked for.
Nothing here claims causation.
"""

from __future__ import annotations

import random
from collections import Counter
from statistics import median

MIN_SAMPLE = 20


def _quantile(sorted_values: list[float], q: float) -> float:
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = (len(sorted_values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def describe(values: list[float | None], *, seed: int = 7, resamples: int = 1000) -> dict:
    present = sorted(v for v in values if v is not None)
    summary = {"n": len(present), "n_missing": len(values) - len(present)}
    if not present:
        return {**summary, "median": None, "p25": None, "p75": None, "median_ci95": None}
    summary.update(
        median=round(median(present), 4),
        p25=round(_quantile(present, 0.25), 4),
        p75=round(_quantile(present, 0.75), 4),
    )
    if len(present) >= 5:
        rng = random.Random(seed)
        medians = sorted(median(rng.choices(present, k=len(present))) for _ in range(resamples))
        summary["median_ci95"] = [round(_quantile(medians, 0.025), 4), round(_quantile(medians, 0.975), 4)]
    else:
        summary["median_ci95"] = None
    return summary


def engagement_rate(metrics: dict) -> float | None:
    """(likes + comments) / followers, only when all three were observed."""
    likes, comments, followers = metrics.get("likes"), metrics.get("comments"), metrics.get("followers")
    if likes is None or comments is None or not followers:
        return None
    return (likes + comments) / followers


def cohort_report(rows: list[dict], *, include_synthetic: bool = False) -> dict:
    """``rows``: one dict per source with ``metrics`` (latest snapshot), ``features``, ``is_synthetic``."""
    used = [r for r in rows if include_synthetic or not r.get("is_synthetic")]
    rates = [engagement_rate(r.get("metrics") or {}) for r in used]
    views = [(r.get("metrics") or {}).get("views") for r in used]
    archetypes = Counter(
        (r.get("features") or {}).get("hook_archetype") or "unclassified" for r in used
    )
    measured = [r["features"] for r in used if (r.get("features") or {}).get("extraction_scope") == "rights_verified_media"]
    sequences = Counter(" > ".join(f["beat_sequence"]) for f in measured if f.get("beat_sequence"))

    caveats = []
    if len(used) < MIN_SAMPLE:
        caveats.append(f"Under-sampled: {len(used)} records (want >= {MIN_SAMPLE}). Treat patterns as anecdotes.")
    if len(measured) < 5:
        caveats.append(f"Only {len(measured)} rights-verified videos have measured timing; pacing stats are thin.")
    caveats.append("Correlational only: public metrics do not show why a video performed.")

    return {
        "sample_size": len(used),
        "synthetic_excluded": 0 if include_synthetic else len(rows) - len(used),
        "engagement_rate": describe(rates),
        "views": describe(views),
        "hook_archetypes": dict(archetypes.most_common()),
        "measured_videos": len(measured),
        "speech_wpm": describe([f.get("speech_wpm") for f in measured]),
        "median_shot_seconds": describe([f.get("median_shot_seconds") for f in measured]),
        "silence_ratio": describe([f.get("silence_ratio") for f in measured]),
        "beat_sequences": dict(sequences.most_common(10)),
        "caveats": caveats,
    }

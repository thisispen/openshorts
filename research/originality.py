"""Lexical overlap checks between an original draft and stored reference text.

One signal, not proof of originality: semantic paraphrase needs a separate
check and a human reviewer. Thresholds are deliberately strict for short-form
scripts, where a single reused hook line is most of the hook.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

MAX_NGRAM_CONTAINMENT = 0.08  # share of the draft's 5-grams found in one reference
MAX_SHARED_RUN_WORDS = 7  # longest verbatim word run allowed
MAX_HOOK_SIMILARITY = 0.6  # token-set Jaccard between draft hook and a reference hook
MAX_DRAFT_SIBLING_SIMILARITY = 0.7  # near-duplicate variants of the same idea


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def _ngrams(words: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def ngram_containment(draft: str, reference: str, n: int = 5) -> float:
    d = _ngrams(tokens(draft), n)
    if not d:
        return 0.0
    return len(d & _ngrams(tokens(reference), n)) / len(d)


def longest_shared_run(draft: str, reference: str) -> int:
    a, b = tokens(draft), tokens(reference)
    if not a or not b:
        return 0
    match = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return match.size


def jaccard(a: str, b: str) -> float:
    sa, sb = set(tokens(a)), set(tokens(b))
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def check_originality(
    draft_text: str,
    draft_hook: str,
    references: list[str],
    reference_hooks: list[str],
) -> dict:
    flags = []
    worst_containment, worst_run, worst_hook = 0.0, 0, 0.0
    for i, ref in enumerate(references):
        containment = ngram_containment(draft_text, ref)
        run = longest_shared_run(draft_text, ref)
        worst_containment, worst_run = max(worst_containment, containment), max(worst_run, run)
        if containment > MAX_NGRAM_CONTAINMENT:
            flags.append({"reference": i, "check": "ngram_containment", "value": round(containment, 3)})
        if run > MAX_SHARED_RUN_WORDS:
            flags.append({"reference": i, "check": "shared_run_words", "value": run})
    for i, hook in enumerate(reference_hooks):
        similarity = jaccard(draft_hook, hook)
        worst_hook = max(worst_hook, similarity)
        if similarity > MAX_HOOK_SIMILARITY:
            flags.append({"reference_hook": i, "check": "hook_similarity", "value": round(similarity, 3)})
    return {
        "passed": not flags,
        "max_ngram_containment": round(worst_containment, 3),
        "longest_shared_run_words": worst_run,
        "max_hook_similarity": round(worst_hook, 3),
        "flags": flags,
        "method": "lexical_v1",
        "note": "Lexical check only; a human reviewer still approves every script.",
    }


def near_duplicate_pairs(drafts: list[str]) -> list[tuple[int, int, float]]:
    """Pairs of drafts too similar to publish as distinct videos."""
    pairs = []
    for i in range(len(drafts)):
        for j in range(i + 1, len(drafts)):
            similarity = jaccard(drafts[i], drafts[j])
            if similarity > MAX_DRAFT_SIBLING_SIMILARITY:
                pairs.append((i, j, round(similarity, 3)))
    return pairs

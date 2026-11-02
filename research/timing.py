"""Speech and editing rhythm from measured word and cut timestamps.

Input words are ``{"text", "start_ms", "end_ms"}`` dicts (the shape the
research API accepts; `transcribe_backends` word output converts 1:1). Every
number here is measured: with no words, timing comes back ``status: unknown``
rather than being estimated from a caption.
"""

from __future__ import annotations

import re
from statistics import mean, median
from typing import Iterable

PAUSE_MIN_MS = 250
SENTENCE_PAUSE_MS = 700

_TOKEN = re.compile(r"[A-Za-z0-9']+")
_NUMBER = re.compile(r"\d")
_NUMBER_WORDS = {
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "hundred", "thousand", "million", "billion", "percent", "half", "dozen",
}
_FIRST_PERSON = {"i", "i'm", "i've", "i'd", "i'll", "me", "my", "mine", "we", "we're", "our", "us"}
_SECOND_PERSON = {"you", "you're", "you've", "you'd", "you'll", "your", "yours"}
_IMPERATIVES = {
    "stop", "watch", "look", "try", "imagine", "listen", "remember", "never", "don't",
    "do", "save", "follow", "share", "comment", "check", "wait", "think", "ask", "start",
    "use", "make", "take", "let's", "go", "get", "see", "read", "learn",
}


def _clean(word: str) -> str:
    return "".join(_TOKEN.findall(word.lower()))


def normalize_words(words: Iterable[dict]) -> list[dict]:
    out = []
    for word in words:
        text = str(word.get("text", "")).strip()
        start, end = int(word["start_ms"]), int(word["end_ms"])
        if not text or end < start:
            continue
        out.append({"text": text, "start_ms": start, "end_ms": end})
    out.sort(key=lambda w: (w["start_ms"], w["end_ms"]))
    return out


def split_sentences(words: list[dict]) -> list[dict]:
    """Sentence = words up to terminal punctuation or a long pause."""
    sentences: list[dict] = []
    current: list[dict] = []
    for i, word in enumerate(words):
        current.append(word)
        nxt = words[i + 1] if i + 1 < len(words) else None
        ends = word["text"].rstrip('"\')').endswith((".", "?", "!"))
        long_gap = nxt is not None and nxt["start_ms"] - word["end_ms"] >= SENTENCE_PAUSE_MS
        if ends or long_gap or nxt is None:
            text = " ".join(w["text"] for w in current)
            sentences.append({
                "start_ms": current[0]["start_ms"],
                "end_ms": current[-1]["end_ms"],
                "text": text,
                "word_count": len(current),
                "is_question": text.rstrip().endswith("?"),
            })
            current = []
    return sentences


def _syllables(word: str) -> int:
    word = _clean(word)
    if not word:
        return 0
    if word.isdigit():
        return max(1, len(word))
    groups = re.findall(r"[aeiouy]+", word)
    count = len(groups)
    if word.endswith("e") and count > 1 and not word.endswith(("le", "ee")):
        count -= 1
    return max(1, count)


def _speech_union_ms(words: list[dict]) -> int:
    total, cur_start, cur_end = 0, None, None
    for w in words:
        if cur_end is None or w["start_ms"] > cur_end:
            if cur_end is not None:
                total += cur_end - cur_start
            cur_start, cur_end = w["start_ms"], w["end_ms"]
        else:
            cur_end = max(cur_end, w["end_ms"])
    if cur_end is not None:
        total += cur_end - cur_start
    return total


def text_window(words: list[dict], end_ms: int) -> str:
    return " ".join(w["text"] for w in words if w["start_ms"] < end_ms)


def analyze_speech(words: Iterable[dict], duration_ms: int, bin_ms: int = 1000) -> dict:
    words = normalize_words(words)
    if not words:
        return {"status": "unknown", "reason": "no word timestamps supplied"}
    if duration_ms <= 0:
        raise ValueError("duration_ms must be positive")

    tokens = [_clean(w["text"]) for w in words]
    tokens = [t for t in tokens if t]
    sentences = split_sentences(words)
    speech_ms = _speech_union_ms(words)

    gaps = []
    for prev, nxt in zip(words, words[1:]):
        gap = nxt["start_ms"] - prev["end_ms"]
        if gap >= PAUSE_MIN_MS:
            gaps.append({"start_ms": prev["end_ms"], "end_ms": nxt["start_ms"], "ms": gap})

    bins = [0] * max(1, -(-duration_ms // bin_ms))
    for w in words:
        mid = (w["start_ms"] + w["end_ms"]) // 2
        bins[min(len(bins) - 1, mid // bin_ms)] += 1
    wpm_series = [round(count * 60_000 / bin_ms, 1) for count in bins]

    syllables = sum(_syllables(w["text"]) for w in words)
    word_count = len(words)
    sentence_count = max(1, len(sentences))
    # Flesch-Kincaid grade: a rough readability signal, labelled as such.
    fk_grade = 0.39 * (word_count / sentence_count) + 11.8 * (syllables / word_count) - 15.59

    imperative_count = sum(
        1 for s in sentences if _clean(s["text"].split()[0]) in _IMPERATIVES
    )
    numeric = sum(1 for w in words if _NUMBER.search(w["text"]) or _clean(w["text"]) in _NUMBER_WORDS)

    return {
        "status": "measured",
        "word_count": word_count,
        "duration_ms": duration_ms,
        "speech_ms": speech_ms,
        "first_word_ms": words[0]["start_ms"],
        "speech_wpm": round(word_count * 60_000 / duration_ms, 1),
        "articulation_wpm": round(word_count * 60_000 / speech_ms, 1) if speech_ms else None,
        "wpm_series": wpm_series,
        "wpm_bin_ms": bin_ms,
        "syllables_per_second": round(syllables * 1000 / duration_ms, 2),
        "unique_word_ratio": round(len(set(tokens)) / len(tokens), 3) if tokens else None,
        "silence_ratio": round(max(0, duration_ms - speech_ms) / duration_ms, 4),
        "pause_count": len(gaps),
        "pauses": gaps,
        "median_pause_ms": median(g["ms"] for g in gaps) if gaps else None,
        "longest_pause_ms": max((g["ms"] for g in gaps), default=None),
        "sentence_count": len(sentences),
        "mean_sentence_words": round(mean(s["word_count"] for s in sentences), 2),
        "question_count": sum(1 for s in sentences if s["is_question"]),
        "imperative_count": imperative_count,
        "numeric_tokens_per_100_words": round(numeric * 100 / word_count, 2),
        "first_person_per_100_words": round(sum(t in _FIRST_PERSON for t in tokens) * 100 / word_count, 2),
        "second_person_per_100_words": round(sum(t in _SECOND_PERSON for t in tokens) * 100 / word_count, 2),
        "reading_grade_estimate": round(fk_grade, 1),
        "hook_0_1s": text_window(words, 1000),
        "hook_0_3s": text_window(words, 3000),
        "sentences": sentences,
    }


def analyze_cuts(cuts_ms: Iterable[int], duration_ms: int) -> dict:
    cuts = sorted({int(c) for c in cuts_ms if 0 < int(c) < duration_ms})
    if not cuts:
        return {"status": "unknown", "reason": "no cut timestamps supplied"}
    edges = [0, *cuts, duration_ms]
    shots = [b - a for a, b in zip(edges, edges[1:])]
    return {
        "status": "measured",
        "cut_count": len(cuts),
        "cuts_ms": cuts,
        "first_cut_ms": cuts[0],
        "shot_count": len(shots),
        "mean_shot_ms": round(mean(shots), 1),
        "median_shot_ms": median(shots),
        "cuts_per_minute": round(len(cuts) * 60_000 / duration_ms, 2),
        # Pattern interrupts: a shot under half the median length.
        "quick_cut_ms": [edges[i] for i, s in enumerate(shots) if s < median(shots) / 2],
    }


def analyze_captions(events: Iterable[dict], duration_ms: int) -> dict:
    events = sorted(events, key=lambda e: e["start_ms"])
    if not events:
        return {"status": "unknown", "reason": "no caption events supplied"}
    words_per_event = [len(e["text"].split()) for e in events]
    shown_ms = sum(max(0, min(e["end_ms"], duration_ms) - e["start_ms"]) for e in events)
    return {
        "status": "measured",
        "event_count": len(events),
        "first_caption_ms": events[0]["start_ms"],
        "mean_words_per_event": round(mean(words_per_event), 2),
        "caption_words_per_second": round(sum(words_per_event) * 1000 / duration_ms, 2),
        "caption_coverage": round(min(1.0, shown_ms / duration_ms), 4),
    }

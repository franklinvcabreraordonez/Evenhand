"""Intent matching: does the purchase fit what the principal actually asked for?

Hard constraints (caps, merchants, time) are deterministic. Intent is the gray zone.
Two heuristic baselines ship here; the real product plugs an LLM or human reviewer in
behind the same interface. Anything the matcher cannot judge must come back as ~0.5
so the case is escalated rather than guessed.
"""
from __future__ import annotations

import re
from typing import Protocol, Sequence

STOP = {
    "a", "an", "the", "for", "my", "me", "to", "of", "and", "buy", "get", "some", "new", "under",
    "around", "than", "less", "cheap", "affordable", "best", "good", "please", "with", "in", "on",
}
SYN = {"sneaker": "shoe", "trainer": "shoe", "notebook": "laptop", "earbud": "headphone"}
PARSE = re.compile(r"^buy (?P<phrase>[a-z0-9 \-]+?) under \$(?P<cap>\d+(\.\d+)?)$", re.I)


def tokens(text: str) -> list[str]:
    out = []
    for w in re.findall(r"[a-z]+", text.lower()):
        if w in STOP:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(SYN.get(w, w))
    return out


class IntentMatcher(Protocol):
    def score(self, intent: str, cart: Sequence[dict]) -> float:
        """Return 0..1. >=hi means match, <=lo means mismatch, in between means ambiguous."""


class KeywordIntentMatcher:
    """Baseline 1: bag-of-words coverage. Brittle by design (this is the thing to beat)."""

    def score(self, intent: str, cart: Sequence[dict]) -> float:
        want = set(tokens(intent))
        if not want or not cart:
            return 0.5
        return max(len(want & set(tokens(it["desc"]))) / len(want) for it in cart)


class HeadNounIntentMatcher(KeywordIntentMatcher):
    """Baseline 2: same, plus a head-noun check, and it abstains on intents it cannot parse."""

    def score(self, intent: str, cart: Sequence[dict]) -> float:
        parsed = PARSE.match(intent.strip())
        if not parsed or not cart:
            return 0.5  # cannot parse -> escalate, do not guess
        want = tokens(parsed.group("phrase"))
        if not want:
            return 0.5
        best = 0.0
        for it in cart:
            have = tokens(it["desc"])
            if not have:
                continue
            cov = len(set(want) & set(have)) / len(set(want))
            if want[-1] != have[-1]:  # different head noun: "laptop bag" is not a "laptop"
                cov = min(cov, 0.2)
            best = max(best, cov)
        return best

# ---------------------------------------------------------------------------------------------
# RobustIntentMatcher: a better baseline. It fixes the three ways valid purchases get misjudged
# when product names are messy (typos, bundles, synonyms) without accepting look-alike products.
# It is still a word-level heuristic. The real answer for hard cases is a model or a human
# behind the same interface.
# ---------------------------------------------------------------------------------------------
GENERIC = {"computer", "device", "product", "item", "set", "pair", "kit", "pack", "bundle", "unit"}
WORD_SYN = {
    "sneaker": "shoe", "trainer": "shoe", "notebook": "laptop", "earbud": "headphone", "headset": "headphone",
    "smartphone": "phone", "cellphone": "phone", "mobile": "phone",
}
# Real retail synonyms. Add to this table as you meet them; it is deliberately small and explicit.
PHRASE_SYN = {
    ("ground", "coffee"): ("coffee", "bean"), ("exercise", "mat"): ("yoga", "mat"),
    ("desk", "chair"): ("office", "chair"), ("computer", "chair"): ("office", "chair"),
}
_SPLIT = re.compile(r"\s*(?:\+|&|,|;|\band\b|\bwith\b|\bplus\b)\s*")


def _phrases(toks: list) -> list:
    out, i = [], 0
    while i < len(toks):
        pair = tuple(toks[i:i + 2])
        if len(pair) == 2 and pair in PHRASE_SYN:
            out.extend(PHRASE_SYN[pair])
            i += 2
        else:
            out.append(toks[i])
            i += 1
    return out


def normalize(text: str) -> list:
    toks = []
    for w in re.findall(r"[a-z]+", text.lower()):
        if w in STOP:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        toks.append(WORD_SYN.get(w, w))
    toks = _phrases(toks)
    if len(toks) > 1:  # "notebook computer" is a laptop; "computer" alone is not dropped
        toks = [t for t in toks if t not in GENERIC] or toks
    return toks


def _within_one_edit(a: str, b: str) -> bool:
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return len(diff) == 2 and diff[1] == diff[0] + 1 and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]]
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def same_word(a: str, b: str) -> bool:
    """Exact, or one typo apart for words of five letters or more (short words must match exactly)."""
    return a == b or (min(len(a), len(b)) >= 5 and _within_one_edit(a, b))


def near_word(a: str, b: str) -> bool:
    """One typo apart for words of four letters or more. Not enough to match, enough to ask a person."""
    return a == b or (min(len(a), len(b)) >= 4 and _within_one_edit(a, b))


class RobustIntentMatcher:
    """Typo-tolerant, bundle-aware, with a head-noun check so look-alike products still fail.

    "runnng shoes" matches "running shoes". "running shoes + running socks" matches, because one item
    in the bundle is what was asked for. "running socks" and "laptop bag" still do not match.
    A possible typo in a short word ("phne" for "phone") is never a confident rejection: it returns 0.5
    so a person decides."""

    def score(self, intent: str, cart: Sequence[dict]) -> float:
        parsed = PARSE.match(intent.strip())
        if not parsed or not cart:
            return 0.5  # cannot parse -> escalate, do not guess
        want = normalize(parsed.group("phrase"))
        if not want:
            return 0.5
        wanted = set(want)
        best = near = 0.0
        for it in cart:
            for segment in _SPLIT.split(it["desc"].lower()):
                have = normalize(segment)
                if not have:
                    continue
                for same, is_best in ((same_word, True), (near_word, False)):
                    cov = sum(any(same(w, h) for h in have) for w in wanted) / len(wanted)
                    if not same(want[-1], have[-1]):  # different head noun: "laptop bag" is not a "laptop"
                        cov = min(cov, 0.2)
                    if is_best:
                        best = max(best, cov)
                    else:
                        near = max(near, cov)
        if best <= 0.25 and near >= 0.5:
            return 0.5  # looks like a typo of the right product: do not reject it automatically
        return best
class LLMIntentMatcher:
    """Stub. Plug in a model call that returns a calibrated 0..1 match score plus rationale."""

    def score(self, intent: str, cart: Sequence[dict]) -> float:  # pragma: no cover
        raise NotImplementedError("wire an LLM or human-review queue here")

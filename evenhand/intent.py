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


class LLMIntentMatcher:
    """Stub. Plug in a model call that returns a calibrated 0..1 match score plus rationale."""

    def score(self, intent: str, cart: Sequence[dict]) -> float:  # pragma: no cover
        raise NotImplementedError("wire an LLM or human-review queue here")

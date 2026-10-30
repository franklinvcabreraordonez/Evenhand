"""Deterministic adjudication of a disputed agent action against its signed mandate."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence

from .intent import HeadNounIntentMatcher, IntentMatcher
from .mandate import Revocation, SignedMandate, verify_mandate, verify_revocation
from .receipts import Receipt, verify_chain


class VerdictType(str, Enum):
    WITHIN_MANDATE = "within_mandate"
    EXCEEDED_MANDATE = "exceeded_mandate"
    AMBIGUOUS_INTENT = "ambiguous_intent"  # escalate to a human / LLM reviewer
    EVIDENCE_INSUFFICIENT = "evidence_insufficient"
    EVIDENCE_INVALID = "evidence_invalid"  # tampered, forged, or unverifiable


AUTO_RESOLVED = {VerdictType.WITHIN_MANDATE, VerdictType.EXCEEDED_MANDATE, VerdictType.EVIDENCE_INVALID}


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Verdict:
    type: VerdictType
    checks: List[Check]
    reasons: List[str]
    intent_score: Optional[float]
    recommendation: str
    chain_head: Optional[str] = None


_RECOMMEND = {
    VerdictType.WITHIN_MANDATE: "Contest the dispute: the signed mandate authorized this action. Attach the evidence packet.",
    VerdictType.EXCEEDED_MANDATE: "Accept the dispute: the agent acted outside its mandate. Route liability per program terms.",
    VerdictType.AMBIGUOUS_INTENT: "Constraints were met but intent is unclear. Send to human review with the packet.",
    VerdictType.EVIDENCE_INSUFFICIENT: "Request the missing witness receipts before deciding.",
    VerdictType.EVIDENCE_INVALID: "Treat as potential fraud or tampering. Escalate; do not rely on submitted logs.",
}


def _finish(vt, checks, reasons, score=None, head=None) -> Verdict:
    return Verdict(vt, checks, reasons, score, _RECOMMEND[vt], head)


def adjudicate(
    signed_mandate: SignedMandate,
    receipts: Sequence[Receipt],
    target_seq: int,
    witness_pub: str,
    *,
    expected_principal_pub: Optional[str] = None,
    revocations: Sequence[Revocation] = (),
    expected_head: Optional[str] = None,
    intent_matcher: Optional[IntentMatcher] = None,
    hi: float = 0.75,
    lo: float = 0.25,
) -> Verdict:
    matcher = intent_matcher or HeadNounIntentMatcher()
    m = signed_mandate.mandate
    c = m.constraints
    checks: List[Check] = []

    # 1. Is the mandate genuine and from the expected principal?
    sig_ok = verify_mandate(signed_mandate)
    key_ok = expected_principal_pub is None or expected_principal_pub == signed_mandate.principal_pub
    checks.append(Check("mandate_signature", sig_ok and key_ok,
                        "verified" if sig_ok and key_ok else "signature invalid or signed by an unexpected key"))
    if not (sig_ok and key_ok):
        return _finish(VerdictType.EVIDENCE_INVALID, checks, ["mandate cannot be authenticated"])

    # 2. Is the witness log intact?
    report = verify_chain(receipts, witness_pub, m.mandate_id, expected_head)
    checks.append(Check("log_integrity", report.ok, "chain intact" if report.ok else "; ".join(report.problems)))
    if not report.ok:
        return _finish(VerdictType.EVIDENCE_INVALID, checks, report.problems, head=report.head)

    # 3. Is the disputed action in the log?
    target = next((r for r in receipts if r.seq == target_seq), None)
    if target is None:
        checks.append(Check("target_present", False, f"receipt {target_seq} not supplied"))
        return _finish(VerdictType.EVIDENCE_INSUFFICIENT, checks, ["disputed action not in supplied log"], head=report.head)
    a = target.action

    # 4. Hard constraints (all evaluated so the packet lists every violation).
    failures: List[str] = []

    def hard(name: str, ok: bool, detail: str) -> None:
        checks.append(Check(name, ok, detail))
        if not ok:
            failures.append(detail)

    hard("agent_binding", a.agent_id == m.agent_id and a.mandate_id == m.mandate_id,
         "action was performed by the authorized agent" if a.agent_id == m.agent_id
         else f"action by {a.agent_id}, mandate names {m.agent_id}")
    hard("action_type", a.action in m.actions, f"action '{a.action}' vs allowed {list(m.actions)}")
    hard("time_window", m.issued_at <= a.ts <= m.expires_at,
         "within validity window" if m.issued_at <= a.ts <= m.expires_at else "action outside mandate validity window")
    revoked = [r for r in revocations
               if verify_revocation(r) and r.principal_pub == signed_mandate.principal_pub
               and r.mandate_id == m.mandate_id and r.revoked_at <= a.ts]
    hard("not_revoked", not revoked, "mandate not revoked before action" if not revoked else "mandate revoked before action")
    if c.max_per_txn is not None:
        hard("per_txn_cap", a.amount <= c.max_per_txn, f"amount {a.amount:.2f} vs cap {c.max_per_txn:.2f}")
    if c.max_total is not None:
        prior = sum(r.action.amount for r in receipts if r.seq < target.seq and r.action.action in m.actions)
        hard("cumulative_cap", prior + a.amount <= c.max_total,
             f"cumulative {prior + a.amount:.2f} vs cap {c.max_total:.2f}")
    if c.merchants_allow is not None:
        hard("merchant_allowed", a.merchant_id in c.merchants_allow, f"merchant {a.merchant_id}")
    hard("merchant_not_denied", a.merchant_id not in c.merchants_deny, f"merchant {a.merchant_id}")
    if c.categories_allow is not None:
        hard("category_allowed", a.category in c.categories_allow, f"category {a.category}")
    if c.step_up_above is not None and a.amount > c.step_up_above:
        hard("step_up", a.step_up_done, f"amount above {c.step_up_above:.2f} requires human confirmation")

    if failures:
        return _finish(VerdictType.EXCEEDED_MANDATE, checks, failures, head=report.head)

    # 5. Intent (the gray zone).
    score = matcher.score(m.intent, [{"desc": it.desc, "price": it.price} for it in a.cart])
    if score >= hi:
        checks.append(Check("intent_match", True, f"score {score:.2f}"))
        return _finish(VerdictType.WITHIN_MANDATE, checks, ["all constraints met; intent consistent"], score, report.head)
    if score <= lo:
        checks.append(Check("intent_match", False, f"score {score:.2f}"))
        return _finish(VerdictType.EXCEEDED_MANDATE, checks, ["purchase does not match stated intent"], score, report.head)
    checks.append(Check("intent_match", False, f"score {score:.2f} (ambiguous)"))
    return _finish(VerdictType.AMBIGUOUS_INTENT, checks, ["constraints met but intent match is unclear"], score, report.head)

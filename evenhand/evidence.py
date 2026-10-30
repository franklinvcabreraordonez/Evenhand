"""Evidence packets: what an issuer, merchant or insurer actually receives."""
from __future__ import annotations

from typing import Optional, Sequence

from .adjudicator import Verdict
from .mandate import SignedMandate
from .receipts import Receipt


def build_packet(case_id: str, sm: SignedMandate, receipts: Sequence[Receipt], target_seq: int,
                 verdict: Verdict, witness_pub: str) -> dict:
    target = next((r for r in receipts if r.seq == target_seq), None)
    return {
        "case_id": case_id,
        "verdict": verdict.type.value,
        "recommendation": verdict.recommendation,
        "reasons": verdict.reasons,
        "intent_score": verdict.intent_score,
        "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in verdict.checks],
        "mandate": {"id": sm.mandate.mandate_id, "digest": sm.digest, "principal_pub": sm.principal_pub,
                    "agent_id": sm.mandate.agent_id, "intent": sm.mandate.intent,
                    "constraints": sm.mandate.constraints.__dict__},
        "witness_pub": witness_pub,
        "chain_head": verdict.chain_head,
        "disputed_receipt": None if target is None else {
            "seq": target.seq, "hash": target.hash, "prev_hash": target.prev_hash,
            "action": target.action.to_dict()},
        "receipts_supplied": len(receipts),
    }


def render_markdown(p: dict) -> str:
    lines = [f"# Evidence packet {p['case_id']}", "",
             f"**Verdict:** `{p['verdict']}`", f"**Recommendation:** {p['recommendation']}", "",
             f"**Stated intent:** {p['mandate']['intent']}",
             f"**Mandate digest:** `{p['mandate']['digest'][:16]}...`  **Chain head:** `{(p['chain_head'] or 'n/a')[:16]}...`", "",
             "| Check | Result | Detail |", "|---|---|---|"]
    for c in p["checks"]:
        lines.append(f"| {c['name']} | {'pass' if c['passed'] else 'FAIL'} | {c['detail']} |")
    dr = p["disputed_receipt"]
    if dr:
        a = dr["action"]
        lines += ["", f"**Disputed action (receipt {dr['seq']}):** {a['action']} of {a['amount']:.2f} {a['currency']} "
                      f"at {a['merchant_id']} ({a['category']}), items: "
                      + ", ".join(i["desc"] for i in a["cart"])]
    return "\n".join(lines)

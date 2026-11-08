"""Plain-JSON forms of mandates, receipts and revocations, with strict validation.

Everything the engine verifies can travel as JSON and be rebuilt without loss, so a mandate
signed in one process can be checked in another, or by a client written in another language.
Malformed input raises ValueError with a message that names the offending field.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from .mandate import Constraints, Mandate, Revocation, SignedMandate
from .receipts import AgentAction, CartItem, Receipt

KEY_HEX, SIG_HEX, HASH_HEX = 64, 128, 64  # Ed25519 public key, signature, SHA-256
BUNDLE_FORMAT = "evenhand-bundle-v1"
_HEXDIGITS = set("0123456789abcdef")


def _obj(d: Any, where: str) -> dict:
    if not isinstance(d, dict):
        raise ValueError(f"{where} must be a JSON object")
    return d


def _get(d: dict, key: str, where: str) -> Any:
    if key not in d:
        raise ValueError(f"{where}.{key} is required")
    return d[key]


def _str(d: dict, key: str, where: str, allow_empty: bool = False) -> str:
    v = _get(d, key, where)
    if not isinstance(v, str) or (not v and not allow_empty):
        raise ValueError(f"{where}.{key} must be a non-empty string")
    return v


def _num(d: dict, key: str, where: str, nullable: bool = False, minimum: Optional[float] = None):
    v = _get(d, key, where)
    if v is None and nullable:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ValueError(f"{where}.{key} must be a finite number" + (" or null" if nullable else ""))
    if minimum is not None and v < minimum:
        raise ValueError(f"{where}.{key} must be at least {minimum:g}")
    return v


def _int(d: dict, key: str, where: str, minimum: int = 0) -> int:
    v = _get(d, key, where)
    if isinstance(v, bool) or not isinstance(v, int) or v < minimum:
        raise ValueError(f"{where}.{key} must be an integer of at least {minimum}")
    return v


def _bool(d: dict, key: str, where: str) -> bool:
    v = _get(d, key, where)
    if not isinstance(v, bool):
        raise ValueError(f"{where}.{key} must be true or false")
    return v


def _hex(d: dict, key: str, where: str, length: int) -> str:
    v = _get(d, key, where)
    if not isinstance(v, str) or len(v) != length or any(c not in _HEXDIGITS for c in v):
        raise ValueError(f"{where}.{key} must be {length} lowercase hex characters")
    return v


def _strs(d: dict, key: str, where: str, nullable: bool = False):
    v = _get(d, key, where)
    if v is None and nullable:
        return None
    if not isinstance(v, (list, tuple)) or any(not isinstance(x, str) for x in v):
        raise ValueError(f"{where}.{key} must be a list of strings" + (" or null" if nullable else ""))
    return tuple(v)


# ---------- mandates ----------
def mandate_to_dict(m: Mandate) -> dict:
    return m.to_dict()


def mandate_from_dict(d: Any) -> Mandate:
    w = "mandate"
    d = _obj(d, w)
    c = _obj(_get(d, "constraints", w), "mandate.constraints")
    cw = "mandate.constraints"
    constraints = Constraints(
        max_per_txn=_num(c, "max_per_txn", cw, nullable=True, minimum=0),
        max_total=_num(c, "max_total", cw, nullable=True, minimum=0),
        currency=_str(c, "currency", cw),
        merchants_allow=_strs(c, "merchants_allow", cw, nullable=True),
        merchants_deny=_strs(c, "merchants_deny", cw),
        categories_allow=_strs(c, "categories_allow", cw, nullable=True),
        step_up_above=_num(c, "step_up_above", cw, nullable=True, minimum=0),
    )
    return Mandate(
        mandate_id=_str(d, "mandate_id", w), principal_id=_str(d, "principal_id", w), agent_id=_str(d, "agent_id", w),
        issued_at=_num(d, "issued_at", w), expires_at=_num(d, "expires_at", w),
        actions=_strs(d, "actions", w), constraints=constraints, intent=_str(d, "intent", w, allow_empty=True),
    )


def signed_mandate_to_dict(sm: SignedMandate) -> dict:
    return {"mandate": mandate_to_dict(sm.mandate), "principal_pub": sm.principal_pub, "signature": sm.signature}


def signed_mandate_from_dict(d: Any) -> SignedMandate:
    w = "signed_mandate"
    d = _obj(d, w)
    return SignedMandate(mandate_from_dict(_get(d, "mandate", w)), _hex(d, "principal_pub", w, KEY_HEX), _hex(d, "signature", w, SIG_HEX))


def revocation_to_dict(r: Revocation) -> dict:
    return {"mandate_id": r.mandate_id, "revoked_at": r.revoked_at, "principal_pub": r.principal_pub, "signature": r.signature}


def revocation_from_dict(d: Any) -> Revocation:
    w = "revocation"
    d = _obj(d, w)
    return Revocation(_str(d, "mandate_id", w), _num(d, "revoked_at", w), _hex(d, "principal_pub", w, KEY_HEX), _hex(d, "signature", w, SIG_HEX))


# ---------- actions and receipts ----------
def action_to_dict(a: AgentAction) -> dict:
    return a.to_dict()


def action_from_dict(d: Any, where: str = "action") -> AgentAction:
    d = _obj(d, where)
    cart_raw = _get(d, "cart", where)
    if not isinstance(cart_raw, (list, tuple)) or not cart_raw:
        raise ValueError(f"{where}.cart must be a non-empty list")
    cart: List[CartItem] = []
    for i, it in enumerate(cart_raw):
        cw = f"{where}.cart[{i}]"
        it = _obj(it, cw)
        cart.append(CartItem(_str(it, "sku", cw), _str(it, "desc", cw, allow_empty=True), _num(it, "price", cw, minimum=0), _int(it, "qty", cw, minimum=1)))
    return AgentAction(
        mandate_id=_str(d, "mandate_id", where), agent_id=_str(d, "agent_id", where), action=_str(d, "action", where),
        amount=_num(d, "amount", where, minimum=0), currency=_str(d, "currency", where), merchant_id=_str(d, "merchant_id", where),
        category=_str(d, "category", where), cart=tuple(cart), step_up_done=_bool(d, "step_up_done", where), ts=_num(d, "ts", where),
    )


def receipt_to_dict(r: Receipt) -> dict:
    return {"seq": r.seq, "prev_hash": r.prev_hash, "action": action_to_dict(r.action),
            "witness_pub": r.witness_pub, "signature": r.signature, "hash": r.hash}


def receipt_from_dict(d: Any) -> Receipt:
    w = "receipt"
    d = _obj(d, w)
    # "hash" is derived data. It is recomputed, never trusted; verify_bundle reports a stated hash that disagrees.
    return Receipt(_int(d, "seq", w), _hex(d, "prev_hash", w, HASH_HEX), action_from_dict(_get(d, "action", w), "receipt.action"),
                   _hex(d, "witness_pub", w, KEY_HEX), _hex(d, "signature", w, SIG_HEX))


# ---------- bundles: everything a third party needs to verify a chain offline ----------
def bundle_to_dict(sm: SignedMandate, receipts: Sequence[Receipt], witness_pub: str,
                   revocations: Sequence[Revocation] = ()) -> dict:
    return {
        "format": BUNDLE_FORMAT,
        "mandate": signed_mandate_to_dict(sm),
        "witness_pub": witness_pub,
        "receipts": [receipt_to_dict(r) for r in receipts],
        "revocations": [revocation_to_dict(r) for r in revocations],
        "head": receipts[-1].hash if receipts else None,
    }


def bundle_from_dict(d: Any):
    """Returns (signed_mandate, receipts, witness_pub, revocations, head)."""
    w = "bundle"
    d = _obj(d, w)
    if d.get("format") != BUNDLE_FORMAT:
        raise ValueError(f"bundle.format must be '{BUNDLE_FORMAT}'")
    receipts_raw = _get(d, "receipts", w)
    if not isinstance(receipts_raw, list):
        raise ValueError("bundle.receipts must be a list")
    revs_raw = d.get("revocations", [])
    if not isinstance(revs_raw, list):
        raise ValueError("bundle.revocations must be a list")
    head = d.get("head")
    if head is not None and (not isinstance(head, str) or len(head) != HASH_HEX):
        raise ValueError("bundle.head must be null or a SHA-256 hex string")
    return (signed_mandate_from_dict(_get(d, "mandate", w)), [receipt_from_dict(r) for r in receipts_raw],
            _hex(d, "witness_pub", w, KEY_HEX), [revocation_from_dict(r) for r in revs_raw], head)
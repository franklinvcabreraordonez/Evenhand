"""Witness an agent in a few lines.

Wrap the function your agent calls to spend money. Every successful call is signed by a
witness and chained to the one before it. When a purchase is disputed, ask for a verdict and
an evidence packet, or export a bundle that anyone can verify offline.

    signed, principal = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"])
    gw = Gateway(signed, expected_principal_pub=principal.public_hex)

    @gw.tool
    def buy(merchant, amount, description):
        ...  # your real purchase call

In production the witness runs outside the agent's process (a gateway or proxy). Here it runs
beside your code, which is enough to integrate, test and demo the flow.
"""
from __future__ import annotations

import functools
import inspect
import math
import threading
import time
import zlib
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence

from .adjudicator import Verdict, adjudicate
from .crypto import Signer
from .evidence import build_packet
from .mandate import Constraints, Mandate, Revocation, SignedMandate, sign_mandate, verify_mandate
from .receipts import AgentAction, CartItem, Receipt, Witness, verify_chain
from .serialize import bundle_from_dict, bundle_to_dict

_MERCHANT_KEYS = ("merchant", "merchant_id")
_AMOUNT_KEYS = ("amount", "price", "total")
_DESC_KEYS = ("description", "item", "desc", "product")


def quick_mandate(*, intent: str, agent_id: str = "agent-1", merchants: Optional[Sequence[str]] = None,
                  max_per_txn: Optional[float] = None, max_total: Optional[float] = None,
                  categories: Optional[Sequence[str]] = None, step_up_above: Optional[float] = None,
                  hours: float = 24.0, principal: Optional[Signer] = None, mandate_id: str = "m-001",
                  principal_id: str = "user-1", now: Optional[float] = None):
    """A signed mandate for development and demos. In real use the user's own wallet signs it.

    Returns (signed_mandate, principal_signer)."""
    principal = principal or Signer()
    t = time.time() if now is None else now
    m = Mandate(mandate_id, principal_id, agent_id, t - 60, t + hours * 3600, ("purchase",),
                Constraints(max_per_txn=max_per_txn, max_total=max_total,
                            merchants_allow=None if merchants is None else tuple(merchants),
                            categories_allow=None if categories is None else tuple(categories),
                            step_up_above=step_up_above), intent)
    return sign_mandate(m, principal), principal


@dataclass
class Dispute:
    case_id: str
    seq: int
    verdict: Verdict
    packet: dict


def _first(args: Dict[str, Any], keys: Sequence[str]):
    for k in keys:
        if k in args and args[k] is not None:
            return args[k]
    return None


def _default_extract(args: Dict[str, Any]) -> Dict[str, Any]:
    merchant, amount = _first(args, _MERCHANT_KEYS), _first(args, _AMOUNT_KEYS)
    if merchant is None or amount is None:
        raise TypeError("could not find the merchant and amount in this call; name the parameters "
                        "'merchant' and 'amount', or pass extract=... to Gateway.tool")
    return {"merchant": merchant, "amount": amount, "description": _first(args, _DESC_KEYS),
            "category": args.get("category"), "sku": args.get("sku"),
            "step_up_done": bool(args.get("step_up_done") or args.get("confirmed") or False)}


class Gateway:
    """Records every action the agent takes under one mandate, and settles disputes about them."""

    def __init__(self, signed_mandate: SignedMandate, *, witness: Optional[Witness] = None,
                 expected_principal_pub: Optional[str] = None, clock: Callable[[], float] = time.time):
        if not verify_mandate(signed_mandate):
            raise ValueError("the signed mandate does not verify, so it cannot be witnessed")
        if expected_principal_pub is not None and expected_principal_pub != signed_mandate.principal_pub:
            raise ValueError("the mandate was signed by a different key than expected_principal_pub")
        self.signed_mandate = signed_mandate
        self.witness = witness or Witness()
        self.expected_principal_pub = expected_principal_pub
        self._clock = clock
        self._lock = threading.Lock()
        self._cases = 0

    # ----- what has been witnessed -----
    @property
    def mandate(self) -> Mandate:
        return self.signed_mandate.mandate

    @property
    def witness_pub(self) -> str:
        return self.witness.public_hex

    @property
    def receipts(self) -> List[Receipt]:
        return self.witness.chain(self.mandate.mandate_id)

    @property
    def last_receipt(self) -> Optional[Receipt]:
        chain = self.receipts
        return chain[-1] if chain else None

    # ----- recording -----
    def record(self, *, merchant: str, amount: float, description: str = "", category: Optional[str] = None,
               currency: Optional[str] = None, sku: Optional[str] = None, step_up_done: bool = False,
               action: str = "purchase") -> Receipt:
        if not isinstance(merchant, str) or not merchant.strip():
            raise ValueError("merchant must be a non-empty string")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount < 0:
            raise ValueError("amount must be a finite number of zero or more")
        desc = "" if description is None else str(description)
        item = CartItem(sku or f"sku-{zlib.crc32(desc.encode()) % 10**6}", desc, float(amount), 1)
        act = AgentAction(self.mandate.mandate_id, self.mandate.agent_id, action, float(amount),
                          currency or self.mandate.constraints.currency, merchant.strip(),
                          category or "general", (item,), bool(step_up_done), self._clock())
        with self._lock:  # keeps the chain ordered when an agent calls tools from several threads
            return self.witness.record(act)

    def tool(self, fn: Optional[Callable] = None, *, action: str = "purchase", category: Optional[str] = None,
             extract: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None):
        """Decorator: witness every successful call. The function's own result is returned unchanged.

        By default the call's arguments named merchant, amount and description (or item, desc, product)
        are recorded. Pass extract=lambda args: {...} to map any other signature; the dict needs
        merchant and amount and may carry description, category, sku and step_up_done."""
        def wrap(f: Callable):
            sig = inspect.signature(f)
            if extract is None:
                names = set(sig.parameters)
                has_var = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values())
                if not has_var and not (names & set(_MERCHANT_KEYS) and names & set(_AMOUNT_KEYS)):
                    raise TypeError(f"{f.__name__}() has no merchant and amount parameters; name them "
                                    "'merchant' and 'amount', or pass extract=... to Gateway.tool")

            def info(args, kwargs) -> Dict[str, Any]:
                bound = sig.bind(*args, **kwargs)
                bound.apply_defaults()
                got = (extract or _default_extract)(dict(bound.arguments))
                if not got.get("description"):
                    got["description"] = f.__name__.replace("_", " ")
                return got

            def log(got: Dict[str, Any]) -> None:
                self.record(merchant=got["merchant"], amount=got["amount"], description=got.get("description", ""),
                            category=got.get("category") or category, sku=got.get("sku"),
                            step_up_done=bool(got.get("step_up_done")), action=action)

            if inspect.iscoroutinefunction(f):
                @functools.wraps(f)
                async def awrapper(*args, **kwargs):
                    got = info(args, kwargs)
                    result = await f(*args, **kwargs)
                    log(got)
                    return result
                return awrapper

            @functools.wraps(f)
            def wrapper(*args, **kwargs):
                got = info(args, kwargs)
                result = f(*args, **kwargs)
                log(got)
                return result
            return wrapper
        return wrap(fn) if fn is not None else wrap

    # ----- disputes and evidence -----
    def dispute(self, seq: Optional[int] = None, *, revocations: Sequence[Revocation] = ()) -> Dispute:
        """Adjudicate one witnessed action (the latest by default) and build its evidence packet."""
        chain = self.receipts
        if seq is None:
            if not chain:
                raise ValueError("nothing has been witnessed yet")
            seq = chain[-1].seq
        verdict = adjudicate(self.signed_mandate, chain, seq, self.witness_pub,
                             expected_principal_pub=self.expected_principal_pub, revocations=revocations,
                             expected_head=self.witness.head(self.mandate.mandate_id))
        with self._lock:
            self._cases += 1
            case_id = f"case-{self._cases:04d}"
        return Dispute(case_id, seq, verdict, build_packet(case_id, self.signed_mandate, chain, seq, verdict, self.witness_pub))

    def export(self, revocations: Sequence[Revocation] = ()) -> dict:
        """Everything a third party needs to verify this chain offline, as plain JSON-ready data."""
        return bundle_to_dict(self.signed_mandate, self.receipts, self.witness_pub, revocations)


def verify_bundle(bundle: dict, *, expected_principal_pub: Optional[str] = None,
                  expected_witness_pub: Optional[str] = None, expected_head: Optional[str] = None) -> dict:
    """Offline check of an exported bundle. Needs no server.

    Without the expected_* arguments this proves only that the bundle is internally consistent: someone
    could build a fresh, self-consistent chain under their own witness key. To trust a bundle, pin the keys
    you obtained from somewhere else: the user's key (expected_principal_pub) and the witness's key
    (expected_witness_pub). Pass expected_head, a checkpoint the witness published, to also catch
    truncation at the end of a chain. The result includes the keys the bundle claims, so you can compare."""
    try:
        sm, receipts, witness_pub, _revocations, stated_head = bundle_from_dict(bundle)
    except ValueError as exc:
        return {"ok": False, "problems": [f"malformed bundle: {exc}"], "head": None, "receipts": 0,
                "principal_pub": None, "witness_pub": None}
    problems: List[str] = []
    if not verify_mandate(sm):
        problems.append("mandate signature does not verify")
    if expected_principal_pub is not None and expected_principal_pub != sm.principal_pub:
        problems.append("mandate was signed by an unexpected key")
    if expected_witness_pub is not None and expected_witness_pub != witness_pub:
        problems.append("receipts were signed by an unexpected witness key")
    report = verify_chain(receipts, witness_pub, sm.mandate.mandate_id, expected_head)
    problems += report.problems
    for raw, r in zip(bundle["receipts"], receipts):
        if "hash" in raw and raw["hash"] != r.hash:
            problems.append(f"receipt {r.seq}: stated hash does not match its contents")
    if stated_head is not None and stated_head != report.head:
        problems.append("stated head does not match the last receipt")
    return {"ok": not problems, "problems": problems, "head": report.head, "receipts": len(receipts),
            "principal_pub": sm.principal_pub, "witness_pub": witness_pub}
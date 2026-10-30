"""Witness-signed, hash-chained action receipts.

The witness sits outside the agent's trust boundary (gateway/proxy/MCP path) and signs
what it observed. One chain per mandate. A third party can verify a chain offline with
only the witness public key.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from .crypto import Signer, canonical, sha256_hex, verify

GENESIS = "0" * 64


@dataclass(frozen=True)
class CartItem:
    sku: str
    desc: str
    price: float
    qty: int = 1


@dataclass(frozen=True)
class AgentAction:
    mandate_id: str
    agent_id: str
    action: str  # e.g. "purchase"
    amount: float
    currency: str
    merchant_id: str
    category: str
    cart: Tuple[CartItem, ...]
    step_up_done: bool
    ts: float

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Receipt:
    seq: int
    prev_hash: str
    action: AgentAction
    witness_pub: str
    signature: str

    def body(self) -> dict:
        return {
            "seq": self.seq,
            "prev_hash": self.prev_hash,
            "action": self.action.to_dict(),
            "witness_pub": self.witness_pub,
        }

    @property
    def hash(self) -> str:
        return sha256_hex(canonical(self.body()) + bytes.fromhex(self.signature))


class Witness:
    """A neutral recorder. In production this is a hosted service with published checkpoints."""

    def __init__(self, signer: Optional[Signer] = None):
        self.signer = signer or Signer()
        self._chains: Dict[str, List[Receipt]] = {}

    @property
    def public_hex(self) -> str:
        return self.signer.public_hex

    def record(self, action: AgentAction) -> Receipt:
        chain = self._chains.setdefault(action.mandate_id, [])
        prev = chain[-1].hash if chain else GENESIS
        body = {
            "seq": len(chain),
            "prev_hash": prev,
            "action": action.to_dict(),
            "witness_pub": self.public_hex,
        }
        receipt = Receipt(len(chain), prev, action, self.public_hex, self.signer.sign(canonical(body)))
        chain.append(receipt)
        return receipt

    def chain(self, mandate_id: str) -> List[Receipt]:
        return list(self._chains.get(mandate_id, []))

    def head(self, mandate_id: str) -> Optional[str]:
        chain = self._chains.get(mandate_id)
        return chain[-1].hash if chain else None


@dataclass
class ChainReport:
    ok: bool
    problems: List[str]
    head: Optional[str]


def verify_chain(
    receipts: Sequence[Receipt],
    witness_pub: str,
    mandate_id: str,
    expected_head: Optional[str] = None,
) -> ChainReport:
    """Checks signatures, contiguity from genesis, hash links, mandate binding, and (optionally) the head."""
    problems: List[str] = []
    prev = GENESIS
    for i, r in enumerate(receipts):
        if r.witness_pub != witness_pub:
            problems.append(f"receipt {r.seq}: signed by an unexpected witness key")
        if not verify(witness_pub, canonical(r.body()), r.signature):
            problems.append(f"receipt {r.seq}: signature does not verify (content altered or forged)")
        if r.seq != i:
            problems.append(f"position {i} holds seq {r.seq} (gap, deletion or reorder)")
        if r.prev_hash != prev:
            problems.append(f"receipt {r.seq}: broken hash link to previous receipt")
        if r.action.mandate_id != mandate_id:
            problems.append(f"receipt {r.seq}: belongs to a different mandate")
        prev = r.hash
    head = prev if receipts else None
    if expected_head is not None and head != expected_head:
        problems.append("chain head does not match the witness checkpoint (truncated or extended)")
    return ChainReport(not problems, problems, head)

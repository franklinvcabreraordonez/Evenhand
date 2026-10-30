"""A mandate is the principal's signed, scoped, expiring authority for one agent."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Optional, Tuple

from .crypto import Signer, canonical, sha256_hex, verify


@dataclass(frozen=True)
class Constraints:
    max_per_txn: Optional[float] = None
    max_total: Optional[float] = None
    currency: str = "USD"
    merchants_allow: Optional[Tuple[str, ...]] = None  # None = any merchant
    merchants_deny: Tuple[str, ...] = ()
    categories_allow: Optional[Tuple[str, ...]] = None  # None = any category
    step_up_above: Optional[float] = None  # amounts above this need a human confirmation


@dataclass(frozen=True)
class Mandate:
    mandate_id: str
    principal_id: str
    agent_id: str
    issued_at: float
    expires_at: float
    actions: Tuple[str, ...]
    constraints: Constraints
    intent: str  # natural-language statement of what the principal wanted

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SignedMandate:
    mandate: Mandate
    principal_pub: str
    signature: str

    @property
    def digest(self) -> str:
        return sha256_hex(canonical(self.mandate.to_dict()))


def sign_mandate(m: Mandate, principal: Signer) -> SignedMandate:
    return SignedMandate(m, principal.public_hex, principal.sign(canonical(m.to_dict())))


def verify_mandate(sm: SignedMandate) -> bool:
    return verify(sm.principal_pub, canonical(sm.mandate.to_dict()), sm.signature)


@dataclass(frozen=True)
class Revocation:
    mandate_id: str
    revoked_at: float
    principal_pub: str
    signature: str


def _rev_payload(mandate_id: str, revoked_at: float) -> bytes:
    return canonical({"mandate_id": mandate_id, "revoked_at": revoked_at, "type": "revocation"})


def sign_revocation(mandate_id: str, revoked_at: float, principal: Signer) -> Revocation:
    return Revocation(
        mandate_id, revoked_at, principal.public_hex, principal.sign(_rev_payload(mandate_id, revoked_at))
    )


def verify_revocation(r: Revocation) -> bool:
    return verify(r.principal_pub, _rev_payload(r.mandate_id, r.revoked_at), r.signature)

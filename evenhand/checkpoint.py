"""Signed checkpoints: the witness's promise about how long a chain was, and what its last receipt was.

A checkpoint says: "for this mandate I had recorded N receipts, and the last one had this hash."
Save it when you get it. Later, check any exported chain against it. The chain must still hold that
receipt at that position, so receipts cut off the end, or history rewritten, are caught, even though a
shorter or rewritten chain verifies perfectly well on its own.

    from evenhand.checkpoint import verify_bundle_with_checkpoint
    result = verify_bundle_with_checkpoint(bundle, saved_checkpoint, expected_witness_pub=WITNESS_KEY)

What this does not do: a dishonest witness could show different histories to different people, each with
its own valid checkpoints. Catching that needs checkpoints published where everyone sees the same ones.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional, Sequence

from .crypto import Signer, canonical, verify
from .integration import verify_bundle
from .receipts import Receipt
from .serialize import HASH_HEX, KEY_HEX, SIG_HEX, _get, _hex, _int, _obj, _str, bundle_from_dict

CHECKPOINT_FORMAT = "evenhand-checkpoint-v1"


@dataclass(frozen=True)
class Checkpoint:
    mandate_id: str
    count: int          # how many receipts the chain held
    head: str           # the hash of the last of them (the receipt at position count - 1)
    issued_at: int      # whole seconds, the witness's clock
    witness_pub: str
    signature: str

    def payload(self) -> bytes:
        return canonical({"type": "checkpoint", "mandate_id": self.mandate_id, "count": self.count, "head": self.head,
                          "issued_at": self.issued_at, "witness_pub": self.witness_pub})


def make_checkpoint(signer: Signer, mandate_id: str, receipts: Sequence[Receipt], issued_at: int) -> Checkpoint:
    """Sign a checkpoint over a chain. The chain must be non-empty and run from seq 0 without gaps."""
    if not receipts:
        raise ValueError("a checkpoint needs at least one receipt")
    last = receipts[-1]
    if last.seq != len(receipts) - 1:
        raise ValueError("the chain is not contiguous from seq 0, so it cannot be checkpointed")
    unsigned = Checkpoint(mandate_id, len(receipts), last.hash, int(issued_at), signer.public_hex, "0" * SIG_HEX)
    return Checkpoint(mandate_id, unsigned.count, unsigned.head, unsigned.issued_at, signer.public_hex, signer.sign(unsigned.payload()))


def verify_checkpoint(cp: Checkpoint, expected_witness_pub: Optional[str] = None) -> bool:
    """True if the signature is valid, and, when given, the witness is the one you expect."""
    if expected_witness_pub is not None and cp.witness_pub != expected_witness_pub:
        return False
    return verify(cp.witness_pub, cp.payload(), cp.signature)


def checkpoint_to_dict(cp: Checkpoint) -> dict:
    return {"format": CHECKPOINT_FORMAT, "mandate_id": cp.mandate_id, "count": cp.count, "head": cp.head,
            "issued_at": cp.issued_at, "witness_pub": cp.witness_pub, "signature": cp.signature}


def checkpoint_from_dict(d: Any) -> Checkpoint:
    w = "checkpoint"
    d = _obj(d, w)
    if d.get("format") != CHECKPOINT_FORMAT:
        raise ValueError(f"checkpoint.format must be '{CHECKPOINT_FORMAT}'")
    return Checkpoint(_str(d, "mandate_id", w), _int(d, "count", w, minimum=1), _hex(d, "head", w, HASH_HEX),
                      _int(d, "issued_at", w, minimum=0), _hex(d, "witness_pub", w, KEY_HEX), _hex(d, "signature", w, SIG_HEX))


def check_against_checkpoint(receipts: Sequence[Receipt], cp: Checkpoint) -> List[str]:
    """Problems found when a chain is held up against a checkpoint. An empty list means it still contains it."""
    if len(receipts) < cp.count:
        return [f"the chain has {len(receipts)} receipts but the checkpoint covers {cp.count}: receipts were cut off"]
    if receipts[cp.count - 1].hash != cp.head:
        return [f"receipt {cp.count - 1} is not the one the checkpoint covers: history was rewritten"]
    return []


def verify_bundle_with_checkpoint(bundle: dict, checkpoint: Any, *, expected_witness_pub: str,
                                  expected_principal_pub: Optional[str] = None) -> dict:
    """verify_bundle, plus: the chain must still contain the receipt the saved checkpoint covers.

    The witness key is required, because a checkpoint from a witness you did not choose proves nothing."""
    result = verify_bundle(bundle, expected_principal_pub=expected_principal_pub, expected_witness_pub=expected_witness_pub)
    problems: List[str] = list(result["problems"])
    out = dict(result, checkpoint_count=None, receipts_after_checkpoint=None)
    try:
        cp = checkpoint if isinstance(checkpoint, Checkpoint) else checkpoint_from_dict(checkpoint)
    except ValueError as exc:
        problems.append(f"malformed checkpoint: {exc}")
        return dict(out, ok=False, problems=problems)
    if cp.witness_pub != expected_witness_pub:
        problems.append("the checkpoint was signed by a different witness key than the one you expected")
    elif not verify_checkpoint(cp, expected_witness_pub):
        problems.append("the checkpoint's signature does not verify")
    try:
        sm, receipts, _w, _r, _h = bundle_from_dict(bundle)
    except ValueError:
        return dict(out, ok=False, problems=problems)  # verify_bundle has already reported the malformed bundle
    if cp.mandate_id != sm.mandate.mandate_id:
        problems.append("the checkpoint is for a different mandate than this bundle")
    else:
        problems += check_against_checkpoint(receipts, cp)
    return dict(out, ok=not problems, problems=problems, checkpoint_count=cp.count,
                receipts_after_checkpoint=max(0, len(receipts) - cp.count))
"""Signing helpers (Ed25519 via the `cryptography` package) and canonical hashing."""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


def canonical(obj) -> bytes:
    """Deterministic JSON bytes: sorted keys, no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Signer:
    """Holds a private key; exposes the public key as hex."""

    def __init__(self, key: Optional[Ed25519PrivateKey] = None):
        self._sk = key or Ed25519PrivateKey.generate()
        self.public_hex = self._sk.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        ).hex()

    def sign(self, payload: bytes) -> str:
        return self._sk.sign(payload).hex()


def verify(public_hex: str, payload: bytes, signature_hex: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(
            bytes.fromhex(signature_hex), payload
        )
        return True
    except (InvalidSignature, ValueError):
        return False

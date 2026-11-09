"""Talking to a witness: a small client for its HTTP API, a witness the proxy can run inside itself, and the
step that registers the user's mandate and reads back what has already been spent under it."""
from __future__ import annotations

import http.client
import json
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional
from urllib.parse import quote, urlsplit

from ..mandate import Mandate, SignedMandate
from ..serialize import bundle_from_dict, signed_mandate_from_dict
from ..server import make_server
from ..store import StoreError
from .lock import FolderInUse, FolderLock


class WitnessError(Exception):
    """The witness refused, or could not be reached."""

    def __init__(self, message: str, status: int = 0, unreachable: bool = False):
        super().__init__(message)
        self.status = status
        self.unreachable = unreachable


class WitnessClient:
    def __init__(self, base_url: str, api_key: Optional[str] = None, timeout: float = 5.0):
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("the witness url must look like http://127.0.0.1:8787")
        self.base_url = base_url.rstrip("/")
        self._scheme, self._host, self._port = parts.scheme, parts.hostname, parts.port
        self._prefix = parts.path.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        headers = {"Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        factory = http.client.HTTPSConnection if self._scheme == "https" else http.client.HTTPConnection
        conn = factory(self._host, self._port, timeout=self._timeout)
        try:
            conn.request(method, self._prefix + path, body=data, headers=headers)
            response = conn.getresponse()
            payload = response.read()
        except (OSError, http.client.HTTPException) as exc:
            raise WitnessError(f"could not reach the witness at {self.base_url}: {exc}", unreachable=True)
        finally:
            conn.close()
        try:
            parsed = json.loads(payload) if payload else None
        except ValueError:
            parsed = None
        if response.status >= 400:
            message = parsed.get("error") if isinstance(parsed, dict) and isinstance(parsed.get("error"), str) else f"HTTP {response.status}"
            raise WitnessError(message, status=response.status)
        if parsed is None:
            raise WitnessError("the witness sent a reply that is not JSON", status=response.status)
        return parsed

    def health(self) -> Dict[str, Any]:
        return self._call("GET", "/healthz")

    def register_mandate(self, signed_mandate: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", "/v1/mandates", signed_mandate)

    def dev_mandate(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", "/v1/dev/mandates", body)

    def record(self, mandate_id: str, action: Dict[str, Any]) -> Dict[str, Any]:
        return self._call("POST", "/v1/receipts", {"mandate_id": mandate_id, "action": action})

    def chain(self, mandate_id: str) -> Dict[str, Any]:
        return self._call("GET", f"/v1/chains/{quote(mandate_id, safe='')}")

    def checkpoint(self, mandate_id: str) -> Dict[str, Any]:
        return self._call("POST", "/v1/checkpoints", {"mandate_id": mandate_id})

    def dispute(self, mandate_id: str, seq: Optional[int] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {"mandate_id": mandate_id}
        if seq is not None:
            body["seq"] = seq
        return self._call("POST", "/v1/disputes", body)

    def packet(self, dispute_id: str) -> Dict[str, Any]:
        return self._call("GET", f"/v1/disputes/{quote(dispute_id, safe='')}/packet")


class EmbeddedWitness:
    """A witness that runs inside this process, on a loopback port only the proxy knows the key to.

    With a data folder it keeps its signing key and everything it records there, so the same witness key
    and the same chain carry on across runs."""

    def __init__(self, data_dir: str, dev: bool = False, principal_keys: Optional[Iterable[str]] = None, timeout: float = 5.0):
        self.api_key = secrets.token_hex(16)
        keys = list(principal_keys) if principal_keys else None
        try:
            self._lock = FolderLock(data_dir)
        except FolderInUse as exc:
            raise WitnessError(f"{exc}. Stop it first, or give each proxy its own --data-dir. To look at a running proxy's record, run it with --witness-url.")
        except OSError as exc:
            raise WitnessError(f"cannot use the data folder {data_dir}: {exc}")
        try:
            self._server = make_server("127.0.0.1", 0, self.api_key, allowed_principals=None if dev else keys, dev=dev, data_dir=data_dir)
        except StoreError as exc:
            self._lock.release()
            raise WitnessError(f"cannot start the witness from {data_dir}: {exc}")
        self._thread = threading.Thread(target=self._server.serve_forever, name="evenhand-witness", daemon=True)
        self._thread.start()
        port = self._server.server_address[1]
        self.client = WitnessClient(f"http://127.0.0.1:{port}", api_key=self.api_key, timeout=timeout)
        self.witness_pub: str = self._server.state.witness.public_hex  # type: ignore[attr-defined]

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._lock.release()


@dataclass
class MandateInfo:
    mandate: Mandate
    signed: SignedMandate
    spent: float
    receipts: int
    witness_pub: str


def open_mandate(client: WitnessClient, *, file: Optional[str] = None, mandate_id: Optional[str] = None,
                 dev: Optional[Dict[str, Any]] = None, principal_key: Optional[str] = None) -> MandateInfo:
    """Registers (or finds) the mandate the proxy will work under, and reads back what is already recorded under it."""
    if file:
        try:
            with open(file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            signed = signed_mandate_from_dict(raw)
        except OSError as exc:
            raise WitnessError(f"cannot read the mandate file {file}: {exc}")
        except ValueError as exc:
            raise WitnessError(f"{file} is not a valid signed mandate: {exc}")
        if principal_key and signed.principal_pub != principal_key:
            raise WitnessError("the mandate was signed by a different key than the principal key you pinned")
        client.register_mandate(raw)
        mandate_id = signed.mandate.mandate_id
    elif dev:
        mandate_id = client.dev_mandate(dev)["mandate_id"]
    if not mandate_id:
        raise WitnessError("no mandate: give a signed mandate file, a mandate id, or a dev mandate")
    bundle = client.chain(mandate_id)
    signed, receipts, witness_pub, _revocations, _head = bundle_from_dict(bundle)
    if principal_key and signed.principal_pub != principal_key:
        raise WitnessError("the mandate was signed by a different key than the principal key you pinned")
    m = signed.mandate
    spent = sum(r.action.amount for r in receipts if r.action.action in m.actions)
    return MandateInfo(m, signed, spent, len(receipts), witness_pub)
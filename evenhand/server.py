"""A minimal HTTP witness, so an agent written in any language can be witnessed.

    python -m evenhand.server --port 8787

Endpoints (JSON in, JSON out):
  GET  /healthz
  POST /v1/mandates              register a signed mandate
  POST /v1/receipts              record an action (the witness stamps the time, not the client)
  GET  /v1/chains/{mandate_id}   export a bundle for offline verification
  GET  /v1/verify?mandate_id=..  check the witness's own chain
  POST /v1/disputes              adjudicate one recorded action
  GET  /v1/disputes/{id}         the verdict and its status
  GET  /v1/disputes/{id}/packet  the evidence packet
  POST /v1/checkpoints           sign a checkpoint: "this chain held N receipts, the last with this hash"
  GET  /v1/checkpoints/{mandate_id}  the latest checkpoint issued for a mandate

With --data-dir the witness keeps its signing key and everything it records in that folder, and picks up where it
left off after a restart. It refuses to start if the saved log no longer verifies.

Development helpers (only with --dev, never for real money):
  POST /v1/dev/mandates          create, sign and register a mandate with a throwaway user key
  POST /v1/dev/revocations       sign a revocation for a mandate made that way

Prototype limits: without --data-dir, state is in memory and the witness key is new each run; unless you pass
--principal-key, the first key to register a mandate is trusted as its principal; there is one bearer token at
most and no TLS; bind to localhost and do not expose it.
"""
from __future__ import annotations

import argparse
import hmac
import json
import math
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .adjudicator import VerdictType, adjudicate
from .checkpoint import (Checkpoint, check_against_checkpoint, checkpoint_from_dict, checkpoint_to_dict, make_checkpoint,
                         verify_checkpoint)
from .crypto import Signer
from .evidence import build_packet
from .integration import quick_mandate
from .mandate import SignedMandate, sign_revocation, verify_mandate
from .receipts import AgentAction, CartItem, Receipt, Witness, verify_chain
from .serialize import (action_from_dict, bundle_to_dict, receipt_from_dict, receipt_to_dict, revocation_from_dict,
                        revocation_to_dict, signed_mandate_from_dict, signed_mandate_to_dict)
from .store import LogCorrupt, Store, StoreError

MAX_BODY = 1_000_000
ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}")
STATUS = {VerdictType.AMBIGUOUS_INTENT: "needs_human_review", VerdictType.EVIDENCE_INSUFFICIENT: "needs_more_evidence"}


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


class WitnessState:
    def __init__(self, api_key: Optional[str] = None, signer: Optional[Signer] = None, clock: Callable[[], float] = time.time,
                 allowed_principals: Optional[Iterable[str]] = None, dev: bool = False, store: Optional[Store] = None):
        if dev and allowed_principals is not None:
            raise ValueError("dev mode signs mandates with throwaway keys, so it cannot be combined with an allow-list of real user keys")
        if store is not None and signer is not None:
            raise ValueError("give either a signer or a store: the store keeps the witness key")
        self.store = store
        self.witness = Witness(store.signer() if store is not None else signer)
        self.api_key, self.clock, self.dev = api_key, clock, dev
        self.allowed_principals = None if allowed_principals is None else frozenset(allowed_principals)
        self.mandates: Dict[str, SignedMandate] = {}
        self.dev_signers: Dict[str, Signer] = {}
        self.disputes: Dict[str, dict] = {}
        self.checkpoints: Dict[str, List[Checkpoint]] = {}
        self.lock = threading.RLock()
        if store is not None:
            self._restore()

    def persist(self, record: dict) -> None:
        """Save a record to disk before the witness replies. If that fails, nothing was recorded."""
        if self.store is None:
            return
        try:
            self.store.append(record)
        except StoreError as exc:
            raise ApiError(503, f"could not save this, so nothing was recorded: {exc}")

    def _restore(self) -> None:
        """Rebuild everything from the saved log, and refuse to start if any of it does not verify."""
        chains: Dict[str, List[Receipt]] = {}
        checkpoints: List[Tuple[str, Checkpoint]] = []
        try:
            for lineno, rec in self.store.open_log():
                where = f"line {lineno} of {self.store.log_path}"
                kind = rec.get("kind")
                try:
                    if kind == "mandate":
                        sm = signed_mandate_from_dict(rec.get("mandate"))
                        if not verify_mandate(sm):
                            raise LogCorrupt(f"{where}: a saved mandate's signature does not verify")
                        self.mandates[sm.mandate.mandate_id] = sm
                    elif kind == "receipt":
                        r = receipt_from_dict(rec.get("receipt"))
                        chains.setdefault(r.action.mandate_id, []).append(r)
                    elif kind == "dispute":
                        d = rec.get("dispute")
                        if not isinstance(d, dict) or not isinstance(d.get("dispute_id"), str) or d["dispute_id"] in self.disputes:
                            raise LogCorrupt(f"{where}: a saved dispute is malformed or repeated")
                        self.disputes[d["dispute_id"]] = d
                    elif kind == "checkpoint":
                        checkpoints.append((where, checkpoint_from_dict(rec.get("checkpoint"))))
                    else:
                        raise LogCorrupt(f"{where}: unknown kind of record {kind!r}")
                except ValueError as exc:
                    raise LogCorrupt(f"{where}: {exc}")
            for mid, chain in chains.items():
                if mid not in self.mandates:
                    raise LogCorrupt(f"the log holds receipts for mandate {mid!r}, which was never registered")
                report = verify_chain(chain, self.witness.public_hex, mid)
                if not report.ok:
                    raise LogCorrupt(f"the saved receipts for mandate {mid!r} do not verify: {report.problems[0]}. If that mentions an "
                                     "unexpected witness key, this log is being used with a different witness.key than it was written with")
                self.witness._chains[mid] = chain
            for where, cp in checkpoints:
                if not verify_checkpoint(cp, self.witness.public_hex):
                    raise LogCorrupt(f"{where}: a saved checkpoint was not signed by this witness")
                problems = check_against_checkpoint(self.witness.chain(cp.mandate_id), cp)
                if problems:
                    raise LogCorrupt(f"{where}: {problems[0]}. The saved log has lost receipts")
                self.checkpoints.setdefault(cp.mandate_id, []).append(cp)
        except StoreError:
            self.store.close()
            raise


def _verdict_json(v) -> dict:
    return {"type": v.type.value, "reasons": v.reasons, "recommendation": v.recommendation, "intent_score": v.intent_score,
            "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in v.checks]}


def _action_from_request(d: Any, sm: SignedMandate, now: float) -> AgentAction:
    """Lenient input for POST /v1/receipts. The witness fills defaults and sets the timestamp itself."""
    if not isinstance(d, dict):
        raise ApiError(400, "action must be a JSON object")
    m = sm.mandate
    merchant = d.get("merchant_id", d.get("merchant"))
    amount = d.get("amount")
    if not isinstance(merchant, str) or not merchant.strip():
        raise ApiError(400, "action.merchant_id (or merchant) must be a non-empty string")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount != amount or amount in (float("inf"), float("-inf")) or amount < 0:
        raise ApiError(400, "action.amount must be a finite number of zero or more")
    cart_raw = d.get("cart")
    if cart_raw is None:
        desc = d.get("description")
        if not isinstance(desc, str):
            raise ApiError(400, "send action.cart (a list of items) or action.description")
        cart_raw = [{"sku": d.get("sku") or "sku-0", "desc": desc, "price": amount, "qty": 1}]
    probe = {"mandate_id": m.mandate_id, "agent_id": d.get("agent_id", m.agent_id), "action": d.get("action", "purchase"),
             "amount": amount, "currency": d.get("currency", m.constraints.currency), "merchant_id": merchant.strip(),
             "category": d.get("category", "general"), "cart": cart_raw, "step_up_done": d.get("step_up_done", False), "ts": now}
    try:
        return action_from_dict(probe, "action")
    except ValueError as exc:
        raise ApiError(400, str(exc))


def _opt_number(d: dict, key: str, minimum: float = 0.0, maximum: float = 1e12):
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < minimum or v > maximum:
        raise ApiError(400, f"{key} must be a finite number between {minimum:g} and {maximum:g}")
    return float(v)


def _opt_strings(d: dict, key: str) -> Optional[List[str]]:
    v = d.get(key)
    if v is None:
        return None
    if isinstance(v, str):
        v = [x.strip() for x in v.split(",") if x.strip()]
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v) or len(v) > 100:
        raise ApiError(400, f"{key} must be a list of non-empty strings (or one comma-separated string)")
    return [x.strip() for x in v]


class Handler(BaseHTTPRequestHandler):
    state: WitnessState  # set by make_server
    _raw: bytes = b""
    server_version = "EvenhandWitness/" + __version__

    def log_message(self, *args):  # keep the console quiet
        pass

    # ----- plumbing -----
    def _send(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_raw(self) -> bytes:
        """Read the whole request body before anything else happens. Replying and closing with unread data makes some
        systems (Windows especially) reset the connection, and then the client never sees the reply."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = -1
        if n < 0 or n > MAX_BODY:
            self.close_connection = True
            raise ApiError(413 if n > MAX_BODY else 400, "request body too large" if n > MAX_BODY else "bad Content-Length")
        return self.rfile.read(n) if n else b""

    def _body(self) -> Any:
        try:
            return json.loads(self._raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, "request body is not valid JSON")

    def _auth(self, path: str) -> None:
        key = self.state.api_key
        if key and path != "/healthz":
            sent = self.headers.get("Authorization", "")
            if not hmac.compare_digest(sent.encode(), f"Bearer {key}".encode()):
                raise ApiError(401, "missing or wrong bearer token")

    def _handle(self, method: str) -> None:
        try:
            self._raw = self._read_raw()
            parts = urlsplit(self.path)
            path, query = parts.path.rstrip("/") or "/", parse_qs(parts.query)
            self._auth(path)
            status, payload = self._route(method, path, query)
            self._send(status, payload)
        except ApiError as exc:
            self._send(exc.status, {"error": exc.message})
        except Exception:  # never leak internals; the witness must keep running
            self._send(500, {"error": "internal error"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    # ----- routes -----
    def _route(self, method: str, path: str, query: Dict[str, List[str]]) -> Tuple[int, Any]:
        s = self.state
        segs = path.strip("/").split("/")
        if method == "GET" and path == "/healthz":
            return 200, {"status": "ok", "witness_pub": s.witness.public_hex, "version": __version__, "dev_mode": s.dev,
                         "principal_allowlist": s.allowed_principals is not None, "persistent": s.store is not None}
        if method == "POST" and path == "/v1/mandates":
            return self._mandates(self._body())
        if method == "POST" and path == "/v1/receipts":
            return self._receipts(self._body())
        if method == "POST" and path == "/v1/disputes":
            return self._dispute(self._body())
        if method == "POST" and path == "/v1/checkpoints":
            return self._checkpoint(self._body())
        if method == "GET" and segs[:2] == ["v1", "checkpoints"] and len(segs) == 3:
            self._mandate(segs[2])
            with s.lock:
                issued = s.checkpoints.get(segs[2])
                if not issued:
                    raise ApiError(404, "no checkpoint has been issued for this mandate yet; create one with POST /v1/checkpoints")
                return 200, checkpoint_to_dict(issued[-1])
        if method == "POST" and path == "/v1/dev/mandates" and s.dev:
            return self._dev_mandate(self._body())
        if method == "POST" and path == "/v1/dev/revocations" and s.dev:
            return self._dev_revocation(self._body())
        if method == "GET" and segs[:2] == ["v1", "chains"] and len(segs) == 3:
            sm = self._mandate(segs[2])
            with s.lock:
                return 200, bundle_to_dict(sm, s.witness.chain(segs[2]), s.witness.public_hex)
        if method == "GET" and path == "/v1/verify":
            mid = (query.get("mandate_id") or [""])[0]
            sm = self._mandate(mid)
            with s.lock:
                chain = s.witness.chain(mid)
                rep = verify_chain(chain, s.witness.public_hex, mid, s.witness.head(mid))
            return 200, {"ok": rep.ok, "problems": rep.problems, "head": rep.head, "receipts": len(chain)}
        if method == "GET" and segs[:2] == ["v1", "disputes"] and len(segs) in (3, 4):
            d = s.disputes.get(segs[2])
            if d is None:
                raise ApiError(404, "unknown dispute")
            if len(segs) == 3:
                return 200, {k: d[k] for k in ("dispute_id", "mandate_id", "seq", "status", "verdict")}
            if segs[3] == "packet":
                return 200, d["packet"]
        raise ApiError(404, "not found")

    def _mandate(self, mandate_id: str) -> SignedMandate:
        sm = self.state.mandates.get(mandate_id)
        if sm is None:
            raise ApiError(404, "unknown mandate_id; register it with POST /v1/mandates first")
        return sm

    def _mandates(self, body: Any) -> Tuple[int, Any]:
        try:
            sm = signed_mandate_from_dict(body)
        except ValueError as exc:
            raise ApiError(400, str(exc))
        if not verify_mandate(sm):
            raise ApiError(422, "the mandate signature does not verify")
        s, mid = self.state, sm.mandate.mandate_id
        if not ID_PATTERN.fullmatch(mid):
            raise ApiError(400, "mandate_id may use letters, digits and . _ : - only, up to 128 characters")
        if s.allowed_principals is not None and sm.principal_pub not in s.allowed_principals:
            raise ApiError(403, "this user key is not on the witness's allow-list")
        with s.lock:
            have = s.mandates.get(mid)
            if have is not None and have.digest != sm.digest:
                raise ApiError(409, "a different mandate is already registered under this mandate_id")
            if have is None:
                s.persist({"kind": "mandate", "mandate": signed_mandate_to_dict(sm)})
                s.mandates[mid] = sm
        return (200 if have is not None else 201), {"mandate_id": mid, "digest": sm.digest, "principal_pub": sm.principal_pub,
                                                    "witness_pub": s.witness.public_hex}

    def _dev_mandate(self, body: Any) -> Tuple[int, Any]:
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        intent = body.get("intent")
        if not isinstance(intent, str) or not intent.strip() or len(intent) > 500:
            raise ApiError(400, "intent must be a non-empty string of at most 500 characters")
        agent_id = body.get("agent_id", "agent-1")
        if not isinstance(agent_id, str) or not ID_PATTERN.fullmatch(agent_id):
            raise ApiError(400, "agent_id may use letters, digits and . _ : - only")
        hours = _opt_number(body, "hours", minimum=0.01, maximum=24 * 365) or 24.0
        s = self.state
        with s.lock:
            mid = body.get("mandate_id")
            if not mid:
                n = 1
                while f"m-dev-{n}" in s.mandates:
                    n += 1
                mid = f"m-dev-{n}"
            if not isinstance(mid, str) or not ID_PATTERN.fullmatch(mid):
                raise ApiError(400, "mandate_id may use letters, digits and . _ : - only, up to 128 characters")
            if mid in s.mandates:
                raise ApiError(409, "a mandate is already registered under this mandate_id")
            sm, principal = quick_mandate(intent=intent.strip(), agent_id=agent_id, merchants=_opt_strings(body, "merchants"),
                                          max_per_txn=_opt_number(body, "max_per_txn"), max_total=_opt_number(body, "max_total"),
                                          categories=_opt_strings(body, "categories"), step_up_above=_opt_number(body, "step_up_above"),
                                          hours=hours, mandate_id=mid, principal_id="dev-user")
            s.persist({"kind": "mandate", "mandate": signed_mandate_to_dict(sm)})
            s.mandates[mid] = sm
            s.dev_signers[mid] = principal
        return 201, {"mandate_id": mid, "digest": sm.digest, "principal_pub": sm.principal_pub, "witness_pub": s.witness.public_hex,
                     "signed_mandate": signed_mandate_to_dict(sm),
                     "warning": "development only: this server holds the user's signing key, which defeats the purpose of a user-held key"}

    def _dev_revocation(self, body: Any) -> Tuple[int, Any]:
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        mid = body.get("mandate_id")
        if not isinstance(mid, str):
            raise ApiError(400, "mandate_id is required")
        s = self.state
        signer = s.dev_signers.get(mid)
        if signer is None:
            raise ApiError(404, "only mandates created with POST /v1/dev/mandates can be revoked this way")
        at = _opt_number(body, "revoked_at", minimum=0.0, maximum=1e12)
        rev = sign_revocation(mid, s.clock() if at is None else at, signer)
        return 201, {"revocation": revocation_to_dict(rev), "next": "send it as revocations: [<this revocation>] in POST /v1/disputes"}

    def _checkpoint(self, body: Any) -> Tuple[int, Any]:
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        mid = body.get("mandate_id")
        if not isinstance(mid, str):
            raise ApiError(400, "mandate_id is required")
        self._mandate(mid)
        s = self.state
        with s.lock:
            chain = s.witness.chain(mid)
            if not chain:
                raise ApiError(409, "nothing has been recorded under this mandate yet, so there is nothing to checkpoint")
            issued = s.checkpoints.setdefault(mid, [])
            if issued and (issued[-1].count, issued[-1].head) == (len(chain), chain[-1].hash):
                return 200, checkpoint_to_dict(issued[-1])  # nothing new since the last one
            cp = make_checkpoint(s.witness.signer, mid, chain, int(s.clock()))
            s.persist({"kind": "checkpoint", "checkpoint": checkpoint_to_dict(cp)})
            issued.append(cp)
        return 201, checkpoint_to_dict(cp)

    def _receipts(self, body: Any) -> Tuple[int, Any]:
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        mid = body.get("mandate_id")
        if not isinstance(mid, str):
            raise ApiError(400, "mandate_id is required")
        sm = self._mandate(mid)
        s = self.state
        with s.lock:
            action = _action_from_request(body.get("action"), sm, s.clock())
            receipt = s.witness.record(action)
            try:
                s.persist({"kind": "receipt", "receipt": receipt_to_dict(receipt)})
            except ApiError:
                s.witness._chains[mid].pop()  # not saved, so it never happened
                raise
            return 201, receipt_to_dict(receipt)

    def _dispute(self, body: Any) -> Tuple[int, Any]:
        if not isinstance(body, dict):
            raise ApiError(400, "body must be a JSON object")
        mid = body.get("mandate_id")
        if not isinstance(mid, str):
            raise ApiError(400, "mandate_id is required")
        sm = self._mandate(mid)
        revs_raw = body.get("revocations", [])
        if not isinstance(revs_raw, list):
            raise ApiError(400, "revocations must be a list")
        try:
            revs = [revocation_from_dict(r) for r in revs_raw]
        except ValueError as exc:
            raise ApiError(400, str(exc))
        s = self.state
        with s.lock:
            chain = s.witness.chain(mid)
            seq = body.get("seq", chain[-1].seq if chain else None)
            if isinstance(seq, bool) or not isinstance(seq, int):
                raise ApiError(400, "seq must be an integer (or omit it to dispute the latest action)")
            verdict = adjudicate(sm, chain, seq, s.witness.public_hex, expected_principal_pub=sm.principal_pub,
                                 revocations=revs, expected_head=s.witness.head(mid))
            did = f"d-{len(s.disputes) + 1:04d}"
            packet = build_packet(did, sm, chain, seq, verdict, s.witness.public_hex)
            rec = {"dispute_id": did, "mandate_id": mid, "seq": seq, "status": STATUS.get(verdict.type, "decided"),
                   "verdict": _verdict_json(verdict), "packet": packet}
            s.persist({"kind": "dispute", "dispute": rec})
            s.disputes[did] = rec
        return 201, {**{k: rec[k] for k in ("dispute_id", "mandate_id", "seq", "status", "verdict")},
                     "packet_url": f"/v1/disputes/{did}/packet"}


def make_server(host: str = "127.0.0.1", port: int = 0, api_key: Optional[str] = None, state: Optional[WitnessState] = None,
                allowed_principals: Optional[Iterable[str]] = None, dev: bool = False,
                data_dir: Optional[str] = None) -> ThreadingHTTPServer:
    st = state or WitnessState(api_key=api_key, allowed_principals=allowed_principals, dev=dev,
                               store=Store(data_dir) if data_dir else None)
    handler = type("BoundHandler", (Handler,), {"state": st})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    server.state = st  # type: ignore[attr-defined]
    stop_listening = server.server_close

    def close() -> None:
        stop_listening()
        if st.store is not None:
            st.store.close()

    server.server_close = close  # type: ignore[method-assign]
    return server


def main() -> None:
    ap = argparse.ArgumentParser(description="Run a prototype Evenhand witness over HTTP.")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--api-key", default=None, help="require 'Authorization: Bearer <key>' on every request")
    ap.add_argument("--principal-key", action="append", default=None, metavar="HEX",
                    help="accept mandates only from this user public key (repeat the flag for several keys)")
    ap.add_argument("--dev", action="store_true", help="enable /v1/dev/* helpers that sign mandates with throwaway keys (testing only)")
    ap.add_argument("--data-dir", default=None, metavar="FOLDER",
                    help="keep the witness key and everything recorded in this folder, and continue from it after a restart")
    a = ap.parse_args()
    if a.dev and a.principal_key:
        ap.error("--dev signs mandates with throwaway keys, so it cannot be combined with --principal-key")
    try:
        server = make_server(a.host, a.port, a.api_key, allowed_principals=a.principal_key, dev=a.dev, data_dir=a.data_dir)
    except StoreError as exc:
        ap.exit(2, f"Cannot start: {exc}\n")
    state = server.state  # type: ignore[attr-defined]
    print(f"Evenhand witness on http://{a.host}:{server.server_address[1]}")
    print(f"Witness public key (publish this so others can pin it): {state.witness.public_hex}")
    if a.data_dir:
        receipts = sum(len(c) for c in state.witness._chains.values())
        print(f"Saving to {a.data_dir}. Loaded {len(state.mandates)} mandates, {receipts} receipts, "
              f"{sum(len(c) for c in state.checkpoints.values())} checkpoints.")
    else:
        print("Nothing is saved: the witness key and all records are lost when this stops. Use --data-dir to keep them.")
    if a.dev:
        print("DEV MODE: the server signs mandates with throwaway keys. For testing only, never for real money.")
    elif not a.principal_key:
        print("Note: without --principal-key, the first key to register a mandate is trusted as its user.")
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: this prototype has no TLS and minimal auth. Do not expose it to the internet.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
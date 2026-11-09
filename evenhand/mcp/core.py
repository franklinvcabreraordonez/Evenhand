"""The proxy's decisions: which tool calls to hold back, which results to have the witness record, and what
to do when the witness cannot be reached. It works on messages, not on pipes, so every transport can use it."""
from __future__ import annotations

import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import rpc
from .audit import AuditLog
from .config import ConfigError, ProxyConfig, ToolSpec, extract
from .policy import Policy
from .witness import MandateInfo, WitnessClient, WitnessError

PENDING_TTL = 900.0  # seconds a forwarded call may wait for its answer before its reserved spending is let go


@dataclass
class Pending:
    """A watched tool call that has been passed on and has not been answered yet."""

    call_id: str
    request_id: Any
    tool: str
    spec: ToolSpec
    args: Dict[str, Any]
    fields: Dict[str, Any]
    reserved: float
    started: float


@dataclass
class Queued:
    """A purchase that went through but whose receipt could not be recorded yet."""

    call_id: str
    action: Dict[str, Any]


def _stderr(text: str) -> None:
    print(f"[evenhand] {text}", file=sys.stderr, flush=True)


def short(value: Any, limit: int = 300) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class Engine:
    def __init__(self, cfg: ProxyConfig, witness: WitnessClient, info: MandateInfo, audit: AuditLog,
                 log: Callable[[str], None] = _stderr, clock: Callable[[], float] = time.time):
        cfg.validate()
        if cfg.enforce != "off":
            for tool, spec in cfg.tools.items():
                if "amount" in spec.fields and spec.fields["amount"].from_result:
                    raise ConfigError(f"enforce needs the amount before the call runs, but tools.{tool}.amount comes from its result")
        self.cfg, self.witness, self.info, self.audit, self.log, self.clock = cfg, witness, info, audit, log, clock
        self.mandate_id = info.mandate.mandate_id
        self.policy = Policy(info.mandate, spent=info.spent, mode="strict" if cfg.enforce == "strict" else "hard")
        self._queue: List[Queued] = []
        self._queue_lock = threading.Lock()
        self._witness_ok_at = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._recover()

    # ----- starting and stopping -----
    def _recover(self) -> None:
        """Reads the journal left by earlier runs: receipts still waiting to be recorded, and calls that never got an answer."""
        calls: Dict[str, Dict[str, Any]] = {}
        for record in AuditLog.read(self.audit.path):
            cid = record.get("call_id")
            if not isinstance(cid, str):
                continue
            state = calls.setdefault(cid, {"forwarded": False, "queued": None, "done": False})
            kind = record.get("event")
            if kind == "forwarded":
                state["forwarded"] = True
            elif kind == "queued" and isinstance(record.get("action"), dict):
                state["queued"] = record["action"]
            elif kind == "recorded":
                state["queued"], state["done"] = None, True
            elif kind in ("tool_error", "error", "input_required", "other", "unrecorded", "interrupted", "blocked"):
                state["done"] = True
        waiting = [Queued(cid, s["queued"]) for cid, s in calls.items() if s["queued"] is not None]
        unknown = [cid for cid, s in calls.items() if s["forwarded"] and not s["done"] and s["queued"] is None]
        self._queue.extend(waiting)
        for cid in unknown:
            self.audit.event("interrupted", call_id=cid, reason="in flight when the proxy last stopped; the outcome is unknown")
        if waiting:
            self.log(f"{len(waiting)} receipt(s) from an earlier run were not recorded yet; recording them now")
        if unknown:
            self.log(f"{len(unknown)} call(s) were in flight when the proxy last stopped. Whether they went through is unknown; see {self.audit.path}")

    def start(self, retry_every: float = 5.0) -> None:
        """Tries the waiting receipts now, and keeps trying in the background."""
        self.flush_queue()
        self._thread = threading.Thread(target=self._retry_loop, args=(retry_every,), name="evenhand-retry", daemon=True)
        self._thread.start()

    def _retry_loop(self, every: float) -> None:
        while not self._stop.wait(every):
            try:
                self.flush_queue()
            except Exception as exc:  # the loop must survive anything
                self.log(f"retrying receipts failed: {exc}")

    def abandon(self, pending: Dict[Any, Pending], reason: str) -> None:
        """Lets go of calls that will not be answered, such as when the connection they were on has gone."""
        for entry in list(pending.values()):
            self._release(entry)
            self.audit.event("interrupted", call_id=entry.call_id, tool=entry.tool, request_id=entry.request_id, reason=reason)
        pending.clear()

    def close(self, pending: Optional[Dict[Any, Pending]] = None) -> None:
        self._stop.set()
        self.abandon(pending if pending is not None else {}, "the proxy stopped before the call was answered")
        try:
            self.flush_queue()
        except Exception as exc:
            self.log(f"could not record the waiting receipts: {exc}")
        left = len(self._queue)
        if left:
            self.log(f"{left} receipt(s) could not be recorded; they will be tried again the next time the proxy starts")
        self.audit.close()

    # ----- client to server -----
    def screen(self, message: Any, pending: Dict[Any, Pending]) -> Optional[bytes]:
        """Looks at a message on its way to the server. Returns None to let it through, or the reply to give the
        client instead when a purchase must not go ahead. Registers every watched call it lets through."""
        self._sweep(pending)
        items = message if isinstance(message, list) else [message]
        if isinstance(message, dict) and message.get("method") == "notifications/cancelled":
            return None
        outcomes: Dict[int, Any] = {}
        for index, item in enumerate(items):
            name = rpc.tool_call_name(item)
            spec = self.cfg.spec_for(name) if name is not None and rpc.is_request(item) else None
            if spec is None:
                continue
            try:
                outcome = self._screen_call(item, name, spec)
            except Exception as exc:
                self.log(f"could not evaluate {name}: {exc}")
                outcome = rpc.tool_error(item, "Evenhand could not evaluate this purchase, so it was not made.") if self.cfg.enforce != "off" else None
            if outcome is not None:
                outcomes[index] = outcome
        accepted = [o for o in outcomes.values() if isinstance(o, Pending)]
        if len(accepted) < len(outcomes):  # something was blocked
            for entry in accepted:
                self._release(entry)
                self.audit.event("blocked", call_id=entry.call_id, tool=entry.tool, request_id=entry.request_id,
                                 reason="another call in the same batch was blocked")
            if not isinstance(message, list):
                return rpc.dumps(outcomes[0])
            answers: List[Dict[str, Any]] = []
            for index, item in enumerate(items):
                if not rpc.is_request(item):
                    continue
                outcome = outcomes.get(index)
                if outcome is not None and not isinstance(outcome, Pending):
                    answers.append(outcome)
                elif rpc.tool_call_name(item) is not None:
                    answers.append(rpc.tool_error(item, "Evenhand did not run this call because another purchase in the same batch was blocked. Nothing was bought."))
                else:
                    answers.append(rpc.rpc_error(item["id"], -32600, "not run: the batch was blocked"))
            return rpc.dumps(answers)
        for entry in accepted:
            previous = pending.get(rpc.key(entry.request_id))
            if previous is not None:
                self._release(previous)
                self.audit.event("interrupted", call_id=previous.call_id, reason="its request id was used again")
            pending[rpc.key(entry.request_id)] = entry
            self.audit.event("forwarded", call_id=entry.call_id, tool=entry.tool, request_id=entry.request_id,
                             fields={k: entry.fields[k] for k in ("merchant", "amount", "category") if k in entry.fields}, enforce=self.cfg.enforce)
        return None

    def _screen_call(self, request: Dict[str, Any], tool: str, spec: ToolSpec) -> Any:
        """A Pending to register, a reply to give instead, or None to let the call through unwitnessed."""
        args = rpc.tool_arguments(request)
        fields, problems = extract(spec, args, None, final=False)
        call_id = uuid.uuid4().hex[:12]
        if problems:
            reason = "; ".join(problems)
            if self.cfg.enforce != "off":
                self.audit.event("blocked", call_id=call_id, tool=tool, request_id=request["id"], reason=f"could not read the call: {reason}")
                return rpc.tool_error(request, f"Evenhand could not read the merchant and amount of this call ({reason}), so the purchase was not made.")
            self.audit.event("unrecorded", call_id=call_id, tool=tool, request_id=request["id"], reason=reason)
            self.log(f"{tool} was passed through but cannot be witnessed: {reason}")
            return None
        reserved = 0.0
        if self.cfg.enforce != "off":
            decision = self.policy.check(fields["merchant"], fields["amount"], self.clock(), category=fields.get("category", "general"),
                                         description=fields.get("description", ""), currency=fields.get("currency"), reserve=True)
            if not decision.allowed:
                self.audit.event("blocked", call_id=call_id, tool=tool, request_id=request["id"], reason=decision.explain(),
                                 fields={k: fields[k] for k in ("merchant", "amount", "category") if k in fields})
                return rpc.tool_error(request, f"Evenhand blocked this purchase: {decision.explain()}. Nothing was bought. "
                                               "Choose a different item, merchant or amount within what the user allowed.")
            reserved = fields["amount"]
        if self.cfg.on_witness_down == "block" and not self._witness_ok():
            self.policy.release(reserved)
            self.audit.event("blocked", call_id=call_id, tool=tool, request_id=request["id"], reason="the witness could not be reached")
            return rpc.tool_error(request, "Evenhand could not reach its witness, so it did not let this purchase go ahead. Nothing was bought. Try again shortly.")
        return Pending(call_id, request["id"], tool, spec, args, fields, reserved, self.clock())

    def _witness_ok(self) -> bool:
        if self.clock() - self._witness_ok_at < 2.0:
            return True
        try:
            self.witness.health()
        except WitnessError as exc:
            self.log(f"witness unreachable: {exc}")
            return False
        self._witness_ok_at = self.clock()
        return True

    def _sweep(self, pending: Dict[Any, Pending]) -> None:
        if not pending:
            return
        now = self.clock()
        for k, entry in list(pending.items()):
            if now - entry.started > PENDING_TTL and pending.pop(k, None) is not None:
                self._release(entry)
                self.audit.event("interrupted", call_id=entry.call_id, tool=entry.tool, request_id=entry.request_id,
                                 reason=f"no answer within {int(PENDING_TTL // 60)} minutes")

    def _release(self, entry: Pending) -> None:
        if entry.reserved:
            self.policy.release(entry.reserved)

    # ----- server to client -----
    def observe(self, message: Any, pending: Dict[Any, Pending]) -> None:
        """Looks at a message on its way to the client. When it answers a watched call, the receipt is recorded
        now, before the caller releases the message, so the agent never acts on a result that has no receipt."""
        for item in message if isinstance(message, list) else [message]:
            if not rpc.is_response(item):
                continue
            entry = pending.pop(rpc.key(item["id"]), None)
            if entry is None:
                continue
            try:
                self._finish(entry, item)
            except Exception as exc:  # the tool's result must still reach the agent
                self.log(f"could not process the answer to {entry.tool}: {exc}")
                self.audit.event("interrupted", call_id=entry.call_id, tool=entry.tool, request_id=entry.request_id, reason=f"internal error: {short(exc)}")

    def _finish(self, entry: Pending, response: Dict[str, Any]) -> None:
        kind, payload = rpc.outcome(response)
        if kind != "complete":
            self._release(entry)
            self.audit.event(kind, call_id=entry.call_id, tool=entry.tool, request_id=entry.request_id, detail=short(payload))
            return
        fields, problems = extract(entry.spec, entry.args, payload if isinstance(payload, dict) else None)
        if problems:
            self._release(entry)
            reason = "; ".join(problems)
            self.audit.event("unrecorded", call_id=entry.call_id, tool=entry.tool, request_id=entry.request_id, reason=reason)
            self.log(f"{entry.tool} went through but could not be recorded: {reason}")
            return
        action = {k: v for k, v in fields.items() if k in ("merchant", "amount", "description", "category", "currency", "sku")}
        action.setdefault("description", entry.tool)
        try:
            receipt = self.witness.record(self.mandate_id, action)
        except WitnessError as exc:
            self._spent(entry, action["amount"])
            with self._queue_lock:
                self._queue.append(Queued(entry.call_id, action))
            self.audit.event("queued", call_id=entry.call_id, tool=entry.tool, request_id=entry.request_id, action=action, reason=short(exc))
            self.log(f"{entry.tool} went through but the witness could not record it ({exc}); it is queued and will be retried")
            return
        self._spent(entry, action["amount"])
        self.audit.event("recorded", call_id=entry.call_id, tool=entry.tool, request_id=entry.request_id, seq=receipt.get("seq"),
                         receipt_hash=receipt.get("hash"), merchant=action["merchant"], amount=action["amount"])

    def _spent(self, entry: Pending, amount: float) -> None:
        if entry.reserved:
            self.policy.commit(entry.reserved)
        else:
            self.policy.add_spent(amount)

    # ----- receipts that are waiting -----
    @property
    def queued(self) -> int:
        with self._queue_lock:
            return len(self._queue)

    def flush_queue(self) -> int:
        """Tries to record the waiting receipts, oldest first, and stops at the first failure. Returns how many were recorded."""
        done = 0
        while True:
            with self._queue_lock:
                item = self._queue[0] if self._queue else None
            if item is None:
                return done
            try:
                receipt = self.witness.record(self.mandate_id, item.action)
            except WitnessError:
                return done
            with self._queue_lock:
                if self._queue and self._queue[0] is item:
                    self._queue.pop(0)
            self.audit.event("recorded", call_id=item.call_id, seq=receipt.get("seq"), receipt_hash=receipt.get("hash"),
                             merchant=item.action.get("merchant"), amount=item.action.get("amount"), retried=True)
            done += 1
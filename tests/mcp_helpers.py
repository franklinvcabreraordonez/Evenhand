"""Shared pieces for the MCP proxy tests: a stand-in witness, a clock, message builders, and a harness that runs the
real stdio proxy loop over real pipes."""
import json
import os
import queue
import sys
import threading
import time

from evenhand import quick_mandate
from evenhand.mcp.audit import AuditLog
from evenhand.mcp.config import config_from_dict
from evenhand.mcp.core import Engine
from evenhand.mcp.stdio import run_stdio
from evenhand.mcp.witness import MandateInfo, WitnessError

NOW = 1_800_000_000.0
MAPPING = {"merchant": "merchant", "amount": "price", "description": "item", "category": "category"}
DEMO_SERVER = [sys.executable, "-m", "evenhand.mcp.demo_server"]
ECHO_SERVER = [sys.executable, "-u", "-c", "import sys\nfor line in sys.stdin.buffer:\n    sys.stdout.buffer.write(line)\n    sys.stdout.buffer.flush()\n"]


def call(request_id, name="buy_item", **args):
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": args}}


def answer(request_id, **result):
    body = {"content": [{"type": "text", "text": "done"}]}
    body.update(result)
    return {"jsonrpc": "2.0", "id": request_id, "result": body}


def buy(request_id, merchant="stridemart", price=89, item="road running shoes", category="sports"):
    return call(request_id, merchant=merchant, price=price, item=item, category=category)


class FakeWitness:
    """Records what it is given, can be switched off, and can be slow."""

    def __init__(self, delay=0.0):
        self.down = False
        self.delay = delay
        self.recorded = []
        self.recorded_at = []
        self.checks = 0
        self._lock = threading.Lock()

    def health(self):
        self.checks += 1
        if self.down:
            raise WitnessError("down", unreachable=True)
        return {"status": "ok"}

    def record(self, mandate_id, action):
        if self.down:
            raise WitnessError("down", unreachable=True)
        time.sleep(self.delay)
        with self._lock:
            self.recorded.append((mandate_id, action))
            self.recorded_at.append(time.monotonic())
            return {"seq": len(self.recorded) - 1, "hash": f"hash{len(self.recorded) - 1}"}


class Clock:
    def __init__(self):
        self.now = NOW

    def __call__(self):
        return self.now


def mandate_info(spent=0.0, **overrides):
    settings = dict(intent="buy running shoes under $120", merchants=["stridemart"], max_per_txn=120, max_total=200, categories=["sports"], now=NOW,
                    mandate_id="m-core")
    settings.update(overrides)
    signed, _ = quick_mandate(**settings)
    return MandateInfo(signed.mandate, signed, spent, 0, "w" * 64)


def make_engine(directory, witness, enforce="off", down="block", tools=None, clock=None, logs=None, spent=0.0, **overrides):
    cfg = config_from_dict({"tools": tools if tools is not None else {"buy_item": MAPPING, "buy_with_confirmation": MAPPING, "slow_buy": MAPPING},
                            "enforce": enforce, "on_witness_down": down})
    clock = clock or (lambda: NOW)                       # the test mandates are dated for NOW, so enforcement must agree
    audit = AuditLog(os.path.join(directory, "audit.jsonl"), clock=clock)
    return Engine(cfg, witness, mandate_info(spent, **overrides), audit, log=(logs.append if logs is not None else (lambda s: None)), clock=clock), audit


class Harness:
    """Runs run_stdio in a thread, with pipes standing in for the agent host's end."""

    def __init__(self, test, engine, argv, timeout=15):
        self.timeout = timeout
        in_r, in_w = os.pipe()
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        self._to_proxy = os.fdopen(in_w, "wb", buffering=0)
        self._from_proxy = os.fdopen(out_r, "rb")
        self._err_from_proxy = os.fdopen(err_r, "rb")
        proxy_in, proxy_out, proxy_err = os.fdopen(in_r, "rb"), os.fdopen(out_w, "wb"), os.fdopen(err_w, "wb")
        self.lines = queue.Queue()
        self.stderr = []
        self.code = None
        self.pending = None
        self._closed = False

        def run():
            self.code = run_stdio(engine, argv, stdin=proxy_in, stdout=proxy_out, stderr=proxy_err)
            for f in (proxy_out, proxy_err, proxy_in):
                f.close()

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        threading.Thread(target=lambda: [self.lines.put(line) for line in iter(self._from_proxy.readline, b"")] or self.lines.put(None), daemon=True).start()
        threading.Thread(target=lambda: self.stderr.extend(iter(self._err_from_proxy.readline, b"")), daemon=True).start()
        test.addCleanup(self.close)

    def send_raw(self, data):
        self._to_proxy.write(data)

    def send(self, message):
        self.send_raw(json.dumps(message).encode("utf-8") + b"\n")

    def recv_raw(self):
        line = self.lines.get(timeout=self.timeout)
        if line is None:
            raise EOFError("the proxy closed its output")
        return line

    def recv(self):
        return json.loads(self.recv_raw())

    def request(self, message):
        self.send(message)
        return self.recv()

    def finish(self):
        """Closes the proxy's input and waits for it to finish. Returns its exit code."""
        if not self._closed:
            self._closed = True
            self._to_proxy.close()
        self._thread.join(timeout=self.timeout)
        if self._thread.is_alive():
            raise AssertionError("the proxy did not finish")
        return self.code

    def close(self):
        try:
            self.finish()
        except AssertionError:
            pass
        for f in (self._from_proxy, self._err_from_proxy):
            try:
                f.close()
            except OSError:
                pass
"""A scripted agent, for checking that the proxy works on this machine and for showing what it does.

Both commands run the real proxy as a subprocess, wrapped around the demo shop server, and talk to it the way an
agent's host does: JSON-RPC lines over its standard input and output."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from typing import Any, Dict, List, Optional

from .. import verify_bundle
from .audit import AuditLog
from .witness import EmbeddedWitness, open_mandate

INTENT = "buy running shoes under $120"
MAPPING = "buy_item:merchant=merchant,amount=price,description=item,category=category"


class PipeClient:
    """Speaks to a proxy (or any stdio MCP server) as an agent's host would."""

    def __init__(self, argv: List[str]):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._stderr: List[bytes] = []
        self._next = 1
        assert self.proc.stdout is not None and self.proc.stderr is not None
        self._readers = [
            threading.Thread(target=lambda: ([self._lines.put(l) for l in iter(self.proc.stdout.readline, b"")], self._lines.put(None)), daemon=True),
            threading.Thread(target=lambda: self._stderr.extend(iter(self.proc.stderr.readline, b"")), daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    @property
    def stderr_text(self) -> str:
        return b"".join(self._stderr).decode("utf-8", "replace")

    def _send(self, message: Dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 30.0) -> Dict[str, Any]:
        rid = self._next
        self._next += 1
        message: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        while True:
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty:
                raise TimeoutError(f"no answer to {method} within {timeout} seconds. The proxy said:\n{self.stderr_text}")
            if line is None:
                raise EOFError(f"the proxy ended. It said:\n{self.stderr_text}")
            reply = json.loads(line)
            if reply.get("id") == rid and "method" not in reply:
                return reply

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def buy(self, merchant: str, item: str, price: float, category: str = "sports") -> Dict[str, Any]:
        return self.request("tools/call", {"name": "buy_item", "arguments": {"merchant": merchant, "item": item, "price": price, "category": category}})

    def close(self, timeout: float = 20.0) -> int:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
        try:
            code = self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait()
        for reader in self._readers:  # they end when the process's pipes do
            reader.join(timeout=3)
        for pipe in (self.proc.stdout, self.proc.stderr):
            try:
                pipe.close()  # type: ignore[union-attr]
            except OSError:
                pass
        return code


def proxy_argv(data_dir: str, *extra: str) -> List[str]:
    return [sys.executable, "-m", "evenhand.mcp", "run", "--data-dir", data_dir, "--tool", MAPPING, *extra, "--",
            sys.executable, "-m", "evenhand.mcp.demo_server"]


def started_mandate(folder: str) -> str:
    """The mandate the proxy last started under in this folder, from its own log."""
    started = [r for r in AuditLog.read(os.path.join(folder, "audit.jsonl")) if r.get("event") == "started"]
    return str(started[-1]["mandate_id"])


def text_of(reply: Dict[str, Any]) -> str:
    return ((reply.get("result") or {}).get("content") or [{"text": json.dumps(reply.get("error"))}])[0].get("text", "")


def handshake(client: PipeClient) -> None:
    client.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "demo-agent", "version": "0"}})
    client.notify("notifications/initialized")


def selftest() -> int:
    """A ten second check that the proxy, the witness and the demo server work together here. Returns 0 if everything passes."""
    results: List[bool] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append(ok)
        print(f"  [{'ok' if ok else 'FAILED'}] {name}" + ("" if ok else f"\n         {detail}"))

    print("Evenhand MCP proxy self-test")
    folder = tempfile.mkdtemp(prefix="evenhand-selftest-")
    try:
        client = PipeClient(proxy_argv(folder, "--dev-mandate", INTENT, "--merchants", "stridemart", "--max-per-txn", "120", "--categories", "sports",
                                       "--enforce", "hard"))
        try:
            handshake(client)
            tools = client.request("tools/list")["result"]["tools"]
            check("the shop's tools arrive through the proxy unchanged", [t["name"] for t in tools] == ["search_products", "buy_item", "buy_with_confirmation", "slow_buy"])
            good = client.buy("stridemart", "road running shoes", 89)
            check("a purchase within the mandate goes through", "Order ord-0001" in text_of(good), text_of(good))
            bad = client.buy("shadymart", "road running shoes", 79)
            check("a purchase outside the mandate is blocked, and the agent is told why", bad["result"].get("isError") and "not on the user's list" in text_of(bad), text_of(bad))
            over = client.buy("stridemart", "luxury shoes", 500)
            check("a purchase over the limit is blocked", over["result"].get("isError") and "per-purchase limit" in text_of(over), text_of(over))
        except Exception as exc:
            check("the proxy answered", False, str(exc))
        code = client.close()
        check("the proxy shut down cleanly when the agent left", code == 0, f"exit code {code}\n{client.stderr_text}")
        check("the shop took exactly one order", client.stderr_text.count("demo-shop: sold") == 1, client.stderr_text)
        try:
            embedded = EmbeddedWitness(os.path.join(folder, "witness"))
            try:
                mandate_id = started_mandate(folder)
                info = open_mandate(embedded.client, mandate_id=mandate_id)
                bundle = embedded.client.chain(mandate_id)
                result = verify_bundle(bundle, expected_principal_pub=info.signed.principal_pub, expected_witness_pub=embedded.witness_pub)
                check("the witness holds exactly one receipt, and its chain verifies", result["ok"] and result["receipts"] == 1, str(result["problems"]))
                verdict = embedded.client.dispute(mandate_id, 0)["verdict"]["type"]
                check("and the adjudicator agrees that purchase was within the mandate", verdict == "within_mandate", verdict)
            finally:
                embedded.stop()
        except Exception as exc:
            check("the witness's record could be read back", False, str(exc))
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    print("All checks passed." if all(results) else "Some checks FAILED.")
    return 0 if all(results) else 1


def demo(directory: Optional[str] = None, keep: bool = False) -> int:
    """The whole story, in two sessions, with the evidence checked at the end."""
    folder = directory or tempfile.mkdtemp(prefix="evenhand-demo-")
    os.makedirs(folder, exist_ok=True)
    print(f"Evenhand MCP proxy demo. Files go in {folder}\n")
    print(f'The user tells their shopping agent: "{INTENT}", from stridemart only, nothing over $120.\n')
    print("SESSION 1: the proxy only watches. The agent has the demo shop's tools, and every purchase it makes is recorded.")
    client = PipeClient(proxy_argv(folder, "--dev-mandate", INTENT, "--merchants", "stridemart", "--max-per-txn", "120", "--categories", "sports", "--enforce", "off"))
    try:
        handshake(client)
        steps = [("stridemart", "road running shoes", 89.0), ("shadymart", "road running shoes", 79.0), ("stridemart", "road running shoes", 95.0)]
        for merchant, item, price in steps:
            reply = client.buy(merchant, item, price)
            print(f"  agent buys {item} from {merchant} for ${price:.2f}: {text_of(reply)}")
        print("  The agent found a cheaper pair at shadymart, which the user never allowed. Nothing stopped it, but it was witnessed.")
    finally:
        client.close()
    mandate_id = started_mandate(folder)

    print("\nLATER: the user disputes the shadymart charge. The evidence, checked:\n")
    from . import cli
    cli.main(["report", "--data-dir", folder, "--mandate-id", mandate_id, "--save-checkpoint", os.path.join(folder, "checkpoint.json"),
              "--disputes", "all", "--packets", os.path.join(folder, "packets")])

    print("\nSESSION 2: same mandate, but now the proxy enforces it.")
    client = PipeClient(proxy_argv(folder, "--mandate-id", mandate_id, "--enforce", "hard"))
    try:
        handshake(client)
        reply = client.buy("shadymart", "road running shoes", 79.0)
        print(f"  agent tries shadymart again: {text_of(reply)}")
        reply = client.buy("stridemart", "road running shoes", 89.0)
        print(f"  agent buys from stridemart instead: {text_of(reply)}")
    finally:
        client.close()

    print("\nFinally, the chain is checked against the checkpoint saved after session 1:\n")
    code = cli.main(["report", "--data-dir", folder, "--mandate-id", mandate_id, "--checkpoint-file", os.path.join(folder, "checkpoint.json")])
    print(f"\nThe evidence packets (JSON{' and PDF' if _has_reportlab() else ''}) are in {os.path.join(folder, 'packets')}")
    if not keep and directory is None:
        print("(Run again with --keep to keep these files.)")
        shutil.rmtree(folder, ignore_errors=True)
    return code


def _has_reportlab() -> bool:
    try:
        import reportlab  # noqa: F401
    except ImportError:
        return False
    return True
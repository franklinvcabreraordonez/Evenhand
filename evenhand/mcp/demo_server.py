"""A tiny MCP server that sells things, for trying the proxy and for its tests.

It speaks the newline-delimited JSON-RPC of the stdio transport in either era of the protocol: the older
initialize handshake, or the newer stateless form where every request carries its own version.
Run it directly:  python -m evenhand.mcp.demo_server"""
from __future__ import annotations

import json
import sys
import time
from typing import Any, Dict, List, Optional

from .rpc import is_modern

CATALOG: Dict[str, List[Dict[str, Any]]] = {
    "stridemart": [{"item": "road running shoes", "price": 89}, {"item": "trail running shoes", "price": 129}, {"item": "running socks", "price": 12.5}],
    "shadymart": [{"item": "road running shoes", "price": 79}, {"item": "gaming console", "price": 399}],
}

TOOLS: List[Dict[str, Any]] = [
    {"name": "search_products", "description": "Search the shops for an item.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "buy_item", "description": "Buy an item from a merchant.",
     "inputSchema": {"type": "object", "properties": {"merchant": {"type": "string"}, "item": {"type": "string"}, "price": {"type": "number"},
                                                      "category": {"type": "string"}}, "required": ["merchant", "item", "price"]}},
    {"name": "buy_with_confirmation", "description": "Buy an item after the server has asked for confirmation.",
     "inputSchema": {"type": "object", "properties": {"merchant": {"type": "string"}, "item": {"type": "string"}, "price": {"type": "number"}},
                     "required": ["merchant", "item", "price"]}},
    {"name": "slow_buy", "description": "Buy an item, slowly.",
     "inputSchema": {"type": "object", "properties": {"merchant": {"type": "string"}, "item": {"type": "string"}, "price": {"type": "number"},
                                                      "seconds": {"type": "number"}}, "required": ["merchant", "item", "price"]}},
]


class Shop:
    def __init__(self) -> None:
        self.orders = 0

    def reply(self, request: Dict[str, Any], result: Dict[str, Any]) -> Dict[str, Any]:
        if is_modern(request):
            result = {"resultType": "complete", **result}
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}

    def error(self, request: Dict[str, Any], code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request["id"], "error": {"code": code, "message": message}}

    def text(self, request: Dict[str, Any], text: str, is_error: bool = False, **extra: Any) -> Dict[str, Any]:
        result: Dict[str, Any] = {"content": [{"type": "text", "text": text}], **extra}
        if is_error:
            result["isError"] = True
        return self.reply(request, result)

    def handle(self, message: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(message, dict) or "method" not in message:
            return None
        if "id" not in message:
            return None  # a notification
        method, params = message["method"], message.get("params") or {}
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": message["id"], "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                                                                    "serverInfo": {"name": "demo-shop", "version": "0.1.0"}}}
        if method == "server/discover":
            return self.reply(message, {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "serverInfo": {"name": "demo-shop", "version": "0.1.0"}})
        if method == "ping":
            return self.reply(message, {})
        if method == "tools/list":
            return self.reply(message, {"tools": TOOLS})
        if method == "tools/call":
            return self.call(message, params.get("name"), params.get("arguments") or {}, params)
        return self.error(message, -32601, f"Method not found: {method}")

    def call(self, request: Dict[str, Any], name: Any, args: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
        if name == "search_products":
            query = str(args.get("query", "")).lower()
            lines = [f"{shop}: {entry['item']} for ${entry['price']}" for shop, entries in CATALOG.items() for entry in entries if query in entry["item"]]
            return self.text(request, "\n".join(lines) or "No matches.")
        if name in ("buy_item", "buy_with_confirmation", "slow_buy"):
            price = args.get("price")
            if isinstance(price, bool) or not isinstance(price, (int, float)) or price <= 0:
                return self.text(request, "The price must be a positive number.", is_error=True)
            if price > 1000:
                return self.text(request, "Payment declined: over the card limit.", is_error=True)
            if name == "buy_with_confirmation" and "inputResponses" not in params and is_modern(request):
                return self.reply(request, {"resultType": "input_required", "requestState": "demo",
                                            "inputRequests": {"confirm": {"method": "elicitation/create", "params": {"mode": "form", "message": "Confirm the purchase?",
                                                                                                                     "requestedSchema": {"type": "object", "properties": {}}}}}})
            if name == "slow_buy":
                time.sleep(min(float(args.get("seconds", 1)), 10.0))
            self.orders += 1
            order = f"ord-{self.orders:04d}"
            print(f"demo-shop: sold {args.get('item')} to the agent for {price} ({order})", file=sys.stderr, flush=True)
            return self.text(request, f"Bought {args.get('item')} from {args.get('merchant')} for ${price}. Order {order}.",
                             structuredContent={"order_id": order, "total": price, "merchant": args.get("merchant")})
        return self.error(request, -32602, f"Unknown tool: {name}")


def main() -> None:
    shop = Shop()
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        try:
            message = json.loads(raw)
        except ValueError:
            continue
        responses = [r for r in (shop.handle(m) for m in (message if isinstance(message, list) else [message])) if r is not None]
        if responses:
            body = responses if isinstance(message, list) else responses[0]
            out.write(json.dumps(body, separators=(",", ":")).encode("utf-8") + b"\n")
            out.flush()


if __name__ == "__main__":
    main()
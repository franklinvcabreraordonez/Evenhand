"""JSON-RPC 2.0 plumbing for MCP messages: classify them, correlate them, and build the few the proxy writes itself.

The proxy forwards the original bytes of every message it does not need to change, so none of this serializes
anything it merely passes along.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional, Tuple

MODERN_META = "io.modelcontextprotocol/protocolVersion"


def parse(raw: bytes) -> Any:
    """The parsed JSON, or None when the bytes are not JSON. The proxy then forwards them untouched."""
    try:
        return json.loads(raw)
    except ValueError:
        return None


def dumps(message: Any) -> bytes:
    """One message as one line of compact UTF-8 JSON, without the newline."""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def is_request(message: Any) -> bool:
    return isinstance(message, dict) and isinstance(message.get("method"), str) and "id" in message


def is_response(message: Any) -> bool:
    return isinstance(message, dict) and "method" not in message and "id" in message and ("result" in message or "error" in message)


def key(message_id: Any) -> Tuple[str, Any]:
    """A hashable form of a JSON-RPC id. 1 and "1" are different ids."""
    return (type(message_id).__name__, message_id)


def is_modern(request: Dict[str, Any]) -> bool:
    """True when the request carries the stateless-era metadata (MCP revision 2026-07-28 and later)."""
    params = request.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    return isinstance(meta, dict) and MODERN_META in meta


def tool_error(request: Dict[str, Any], text: str) -> Dict[str, Any]:
    """A tool execution error for a request. The model sees it and can adapt, as the specification intends."""
    result: Dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": True}
    if is_modern(request):
        result = {"resultType": "complete", **result}
    return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def rpc_error(message_id: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def tool_call_name(request: Any) -> Optional[str]:
    """The tool a tools/call request names, or None when it is some other message."""
    if isinstance(request, dict) and request.get("method") == "tools/call":
        params = request.get("params")
        if isinstance(params, dict) and isinstance(params.get("name"), str):
            return params["name"]
    return None


def tool_arguments(request: Dict[str, Any]) -> Dict[str, Any]:
    arguments = (request.get("params") or {}).get("arguments")
    return arguments if isinstance(arguments, dict) else {}


def outcome(response: Dict[str, Any]) -> Tuple[str, Any]:
    """What a response to tools/call amounts to.

    "complete"        a finished call that succeeded: the only outcome that means something was done
    "tool_error"      the tool ran and reported an error (isError), so nothing was bought
    "error"           a JSON-RPC protocol error
    "input_required"  the server needs input before it can finish (a retry follows, with a new id)
    "other"           anything else, such as a handle for a long-running task
    The second value is the result (or error) object."""
    if "error" in response:
        return "error", response["error"]
    result = response.get("result")
    if not isinstance(result, dict):
        return "other", result
    if result.get("isError") is True:
        return "tool_error", result
    result_type = result.get("resultType")
    if result_type == "input_required":
        return "input_required", result
    if result_type in (None, "complete") and "task" not in result:
        return "complete", result
    return "other", result
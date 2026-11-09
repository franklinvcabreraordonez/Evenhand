"""Settings for the proxy, and how a tool call's arguments and result become the fields of a receipt."""
from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

_MISSING = object()
DEFAULT_DATA_DIR = os.path.join(os.path.expanduser("~"), ".evenhand-mcp")


class ConfigError(ValueError):
    """The settings are wrong. The message says which one and why."""


# ----- reading values out of a call -----
_SEGMENT = re.compile(r"^([A-Za-z0-9_\-]+)((?:\[\d+\])*)$")


def dig(value: Any, path: str) -> Any:
    """Follows a dotted path with optional [n] indexes, like items[0].name. Returns _MISSING if any step is absent."""
    for segment in path.split("."):
        match = _SEGMENT.match(segment)
        if not match:
            return _MISSING
        if not isinstance(value, dict) or match.group(1) not in value:
            return _MISSING
        value = value[match.group(1)]
        for index in re.findall(r"\[(\d+)\]", match.group(2)):
            if not isinstance(value, list) or int(index) >= len(value):
                return _MISSING
            value = value[int(index)]
    return value


def to_amount(raw: Any, scale: Optional[Decimal]) -> float:
    """A price in major units (dollars). Accepts numbers and numeric strings such as "$1,299.50"."""
    if isinstance(raw, bool) or raw is None:
        raise ValueError("not a number")
    try:
        if isinstance(raw, str):
            raw = raw.strip().replace(",", "").lstrip("$\u00a3\u20ac").strip()
        number = Decimal(str(raw))
    except InvalidOperation:
        raise ValueError("not a number")
    if not number.is_finite():
        raise ValueError("not a finite number")
    if scale is not None:
        number = number * scale
    if number < 0:
        raise ValueError("negative")
    return float(number)


def to_text(raw: Any) -> str:
    """A description from a string, or from a list of strings or of objects with a name, title or description."""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, list):
        parts = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item.strip())
            elif isinstance(item, dict):
                for key in ("name", "title", "description", "desc", "item"):
                    if isinstance(item.get(key), str):
                        parts.append(item[key].strip())
                        break
        return ", ".join(p for p in parts if p)
    raise ValueError("not text")


@dataclass
class Field:
    """Where one value comes from: a path into the call's arguments or its result, or a constant."""

    path: Optional[str] = None
    const: Any = _MISSING
    scale: Optional[Decimal] = None

    @property
    def from_result(self) -> bool:
        return self.path is not None and self.path.startswith("result.")

    def read(self, args: Dict[str, Any], result: Optional[Dict[str, Any]]) -> Any:
        if self.const is not _MISSING:
            return self.const
        assert self.path is not None
        if self.path.startswith("result."):
            return dig(result, self.path[len("result."):]) if result is not None else _MISSING
        return dig(args, self.path[len("args."):] if self.path.startswith("args.") else self.path)


def parse_field(name: str, raw: Any, scale: Any = None) -> Field:
    if isinstance(raw, str) and raw.strip():
        spec = Field(path=raw.strip())
    elif isinstance(raw, dict) and ("path" in raw or "const" in raw):
        spec = Field(path=raw.get("path"), const=raw["const"] if "const" in raw else _MISSING)
        scale = raw.get("scale", scale)
        if spec.path is not None and not isinstance(spec.path, str):
            raise ConfigError(f"{name}: path must be text")
    else:
        raise ConfigError(f'{name}: give a path such as "price", or {{"const": value}}')
    if scale is not None:
        try:
            spec.scale = Decimal(str(scale))
        except InvalidOperation:
            raise ConfigError(f"{name}: scale must be a number such as 0.01")
    return spec


FIELD_NAMES = ("merchant", "amount", "description", "category", "currency", "sku")


@dataclass
class ToolSpec:
    """How to read the merchant, the amount and the rest from one tool's calls. None means: guess from common names."""

    fields: Dict[str, Field] = field(default_factory=dict)

    @property
    def auto(self) -> bool:
        return not self.fields

    @staticmethod
    def from_dict(tool: str, raw: Any) -> "ToolSpec":
        if not isinstance(raw, dict):
            raise ConfigError(f"tools.{tool} must be an object")
        scale = raw.get("amount_scale")
        unknown = set(raw) - set(FIELD_NAMES) - {"amount_scale"}
        if unknown:
            raise ConfigError(f"tools.{tool}: unknown setting {sorted(unknown)[0]!r}")
        fields = {n: parse_field(f"tools.{tool}.{n}", raw[n], scale if n == "amount" else None) for n in FIELD_NAMES if n in raw}
        for needed in ("merchant", "amount"):
            if needed not in fields:
                raise ConfigError(f"tools.{tool} needs a {needed}")
        return ToolSpec(fields)


_AUTO = {
    "merchant": ["merchant", "merchant_id", "merchant.id", "merchant.name", "seller", "store", "store.id", "store.name", "vendor",
                 "payee", "recipient"],
    "amount": ["amount", "price", "total", "cost", "total_price", "amount.value", "price.amount"],
    "amount_minor": ["amount_cents", "price_cents", "total_cents", "amount_minor", "total_minor"],
    "description": ["description", "item", "items", "product", "desc", "memo", "note", "title", "name"],
    "category": ["category"],
    "currency": ["currency"],
    "sku": ["sku", "product_id", "item_id"],
}


def extract(spec: ToolSpec, args: Dict[str, Any], result: Optional[Dict[str, Any]], final: bool = True) -> Tuple[Dict[str, Any], List[str]]:
    """The receipt fields that can be read from a call, and what could not be read.

    Returns (fields, problems). The fields hold merchant, amount (a float in major units) and, when found,
    description, category, currency and sku. With final=False the call has not run yet, so values that come
    from its result are skipped without being reported as missing."""
    fields: Dict[str, Any] = {}
    problems: List[str] = []
    if spec.auto:
        for name, candidates in _AUTO.items():
            for path in candidates:
                raw = dig(args, path)
                if raw is not _MISSING and raw is not None and raw != "":
                    target = "amount" if name == "amount_minor" else name
                    if target not in fields:
                        fields[target] = _convert(target, raw, Decimal("0.01") if name == "amount_minor" else None, path, problems)
                    break
    else:
        for name, source in spec.fields.items():
            if not final and source.from_result:
                continue
            raw = source.read(args, result)
            if raw is _MISSING or raw is None or raw == "":
                if name in ("merchant", "amount"):
                    problems.append(f"{name} (from {source.path}) was not in the call" + (" or its result" if source.from_result else ""))
                continue
            fields[name] = _convert(name, raw, source.scale, source.path or "constant", problems)
    fields = {k: v for k, v in fields.items() if v is not None}
    for needed in ("merchant", "amount"):
        deferred = not final and not spec.auto and spec.fields[needed].from_result
        if needed not in fields and not deferred and not any(p.startswith(needed) for p in problems):
            problems.append(f"{needed} could not be found in the call")
    return fields, problems


def _convert(name: str, raw: Any, scale: Optional[Decimal], where: str, problems: List[str]) -> Any:
    try:
        if name == "amount":
            return to_amount(raw, scale)
        if name in ("description",):
            return to_text(raw)
        if isinstance(raw, (str, int)) and not isinstance(raw, bool):
            return str(raw).strip()
        raise ValueError("not text")
    except ValueError as exc:
        problems.append(f"{name} (from {where}) is {exc}")
        return None


# ----- the settings file -----
@dataclass
class UpstreamConfig:
    command: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    cwd: Optional[str] = None


@dataclass
class ProxyConfig:
    data_dir: str = DEFAULT_DATA_DIR
    witness_url: Optional[str] = None
    witness_key: Optional[str] = None
    api_key: Optional[str] = None
    mandate_file: Optional[str] = None
    mandate_id: Optional[str] = None
    dev_mandate: Optional[Dict[str, Any]] = None
    principal_key: Optional[str] = None
    tools: Dict[str, ToolSpec] = field(default_factory=dict)
    watch: List[str] = field(default_factory=list)
    enforce: str = "off"
    on_witness_down: str = "block"
    witness_timeout: float = 5.0
    upstream: UpstreamConfig = field(default_factory=UpstreamConfig)

    def spec_for(self, tool: str) -> Optional[ToolSpec]:
        """The reading rules for a tool, or None when the proxy should not witness it."""
        if tool in self.tools:
            return self.tools[tool]
        for pattern, spec in self.tools.items():
            if any(ch in pattern for ch in "*?[") and fnmatch.fnmatchcase(tool, pattern):
                return spec
        if any(fnmatch.fnmatchcase(tool, pattern) for pattern in self.watch):
            return ToolSpec()
        return None

    def validate(self) -> None:
        if self.enforce not in ("off", "hard", "strict"):
            raise ConfigError('enforce must be "off", "hard" or "strict"')
        if self.on_witness_down not in ("block", "queue"):
            raise ConfigError('on_witness_down must be "block" or "queue"')
        chosen = [x for x in (self.mandate_file, self.mandate_id, self.dev_mandate) if x]
        if len(chosen) > 1:
            raise ConfigError("give only one of mandate file, mandate id and dev mandate")
        if self.witness_url is not None and not re.match(r"^https?://", self.witness_url):
            raise ConfigError("witness url must start with http:// or https://")
        if self.principal_key is not None and not re.fullmatch(r"[0-9a-f]{64}", self.principal_key):
            raise ConfigError("principal key must be 64 lowercase hex characters")
        if not self.tools and not self.watch:
            raise ConfigError("nothing to witness: name tools in tools, or patterns in watch")


def load_config(path: str) -> ProxyConfig:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}")
    except ValueError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}")
    return config_from_dict(raw)


def config_from_dict(raw: Any) -> ProxyConfig:
    if not isinstance(raw, dict):
        raise ConfigError("the settings must be a JSON object")
    known = {"data_dir", "witness", "mandate", "principal_key", "tools", "watch", "enforce", "on_witness_down", "upstream"}
    if set(raw) - known:
        raise ConfigError(f"unknown setting {sorted(set(raw) - known)[0]!r}")
    cfg = ProxyConfig()
    cfg.data_dir = _text(raw, "data_dir", cfg.data_dir)
    witness = raw.get("witness", {})
    mandate = raw.get("mandate", {})
    if not isinstance(witness, dict) or not isinstance(mandate, dict):
        raise ConfigError("witness and mandate must be objects")
    cfg.witness_url = witness.get("url")
    cfg.witness_key = witness.get("key")
    cfg.api_key = witness.get("api_key")
    cfg.witness_timeout = float(witness.get("timeout", cfg.witness_timeout))
    cfg.mandate_file = mandate.get("file")
    cfg.mandate_id = mandate.get("id")
    cfg.dev_mandate = mandate.get("dev")
    cfg.principal_key = raw.get("principal_key")
    tools = raw.get("tools", {})
    if not isinstance(tools, dict):
        raise ConfigError("tools must be an object")
    cfg.tools = {name: ToolSpec.from_dict(name, spec) for name, spec in tools.items()}
    watch = raw.get("watch", [])
    if not isinstance(watch, list) or not all(isinstance(p, str) for p in watch):
        raise ConfigError("watch must be a list of tool name patterns")
    cfg.watch = watch
    cfg.enforce = _text(raw, "enforce", cfg.enforce)
    cfg.on_witness_down = _text(raw, "on_witness_down", cfg.on_witness_down)
    upstream = raw.get("upstream", {})
    if not isinstance(upstream, dict):
        raise ConfigError("upstream must be an object")
    command = upstream.get("command", [])
    if not isinstance(command, list) or not all(isinstance(p, str) for p in command):
        raise ConfigError("upstream.command must be a list of strings")
    cfg.upstream = UpstreamConfig(command, dict(upstream.get("env", {})), upstream.get("cwd"))
    return cfg


def _text(raw: Dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be text")
    return value
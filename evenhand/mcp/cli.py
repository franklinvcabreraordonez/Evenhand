"""The evenhand-mcp command line.

    evenhand-mcp run [options] -- <the real MCP server command>      witness a local (stdio) tool server
    evenhand-mcp serve-http --upstream-url URL [options]              witness a remote (Streamable HTTP) tool server
    evenhand-mcp keygen --out user.key                                make the user's signing key
    evenhand-mcp mandate create --key user.key --intent "..." ...     sign a mandate to a file
    evenhand-mcp report --data-dir DIR                                see what was recorded, verify it, and judge disputes
    evenhand-mcp selftest | demo                                      check this installation, or watch the whole story

Nothing but protocol messages is ever written to standard output while a proxy runs. Messages for people go to standard error."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .. import quick_mandate, verify_bundle
from ..checkpoint import verify_bundle_with_checkpoint
from ..crypto import Signer
from ..serialize import signed_mandate_to_dict
from .audit import AuditLog
from .config import DEFAULT_DATA_DIR, FIELD_NAMES, ConfigError, ProxyConfig, ToolSpec, config_from_dict, load_config
from .core import Engine
from .witness import EmbeddedWitness, MandateInfo, WitnessClient, WitnessError, open_mandate


def say(text: str) -> None:
    print(f"[evenhand] {text}", file=sys.stderr, flush=True)


# ----- keys and mandates -----
def load_signer(path: str) -> Signer:
    try:
        with open(path, "r", encoding="ascii") as f:
            seed = bytes.fromhex(f.read().strip())
        if len(seed) != 32:
            raise ValueError("not 32 bytes")
        return Signer(Ed25519PrivateKey.from_private_bytes(seed))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path} is not a usable key file ({exc})")


def new_key_file(path: str) -> Signer:
    seed = os.urandom(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(seed.hex() + "\n")
    except FileExistsError:
        raise ConfigError(f"{path} already exists, and a key is never overwritten")
    except OSError as exc:
        raise ConfigError(f"cannot write {path}: {exc}")
    return Signer(Ed25519PrivateKey.from_private_bytes(seed))


def cmd_keygen(args: argparse.Namespace) -> int:
    signer = new_key_file(args.out)
    print(f"Wrote the user's private key to {args.out}. Keep it secret and back it up.")
    print(f"Public key (share this, and pin it when verifying): {signer.public_hex}")
    return 0


def _csv(text: Optional[str]) -> Optional[List[str]]:
    return None if text is None else [p.strip() for p in text.split(",") if p.strip()]


def cmd_mandate_create(args: argparse.Namespace) -> int:
    signer = load_signer(args.key)
    signed, _ = quick_mandate(intent=args.intent, agent_id=args.agent_id, merchants=_csv(args.merchants), max_per_txn=args.max_per_txn,
                              max_total=args.max_total, categories=_csv(args.categories), step_up_above=args.step_up_above, hours=args.hours,
                              principal=signer, mandate_id=args.mandate_id, principal_id=args.user_id)
    try:
        with open(args.out, "x", encoding="utf-8") as f:
            json.dump(signed_mandate_to_dict(signed), f, indent=2)
    except FileExistsError:
        raise ConfigError(f"{args.out} already exists")
    m = signed.mandate
    print(f"Signed mandate {m.mandate_id} for agent {m.agent_id}, valid until {time.strftime('%Y-%m-%d %H:%M', time.localtime(m.expires_at))}.")
    print(f"Written to {args.out}. The user's public key is {signer.public_hex}")
    return 0


# ----- settings from flags -----
def parse_tool(text: str) -> Tuple[str, ToolSpec]:
    """name:merchant=PATH,amount=PATH,description=PATH,amount_scale=0.01,category=const:sports"""
    name, _, rest = text.partition(":")
    if not name or not rest:
        raise ConfigError(f"--tool {text!r}: write it as name:merchant=PATH,amount=PATH")
    raw: Dict[str, Any] = {}
    for pair in rest.split(","):
        key, _, value = pair.partition("=")
        key = key.strip()
        if key not in FIELD_NAMES + ("amount_scale",) or not value:
            raise ConfigError(f"--tool {text!r}: {key or pair!r} is not understood")
        raw[key] = {"const": value[len("const:"):]} if value.startswith("const:") else value.strip()
    return name, ToolSpec.from_dict(name, raw)


def build_config(args: argparse.Namespace) -> ProxyConfig:
    cfg = load_config(args.config) if args.config else config_from_dict({})
    for attr, flag in (("data_dir", "data_dir"), ("witness_url", "witness_url"), ("witness_key", "witness_key"), ("api_key", "api_key"),
                       ("mandate_file", "mandate"), ("mandate_id", "mandate_id"), ("principal_key", "principal_key"),
                       ("enforce", "enforce"), ("on_witness_down", "on_witness_down")):
        value = getattr(args, flag, None)
        if value is not None:
            setattr(cfg, attr, value)
    if getattr(args, "dev_mandate", None):
        dev: Dict[str, Any] = {"intent": args.dev_mandate}
        for key, value in (("merchants", _csv(args.merchants)), ("categories", _csv(args.categories)), ("max_per_txn", args.max_per_txn),
                           ("max_total", args.max_total), ("hours", args.hours)):
            if value is not None:
                dev[key] = value
        cfg.dev_mandate = dev
    for text in getattr(args, "tool", None) or []:
        name, spec = parse_tool(text)
        cfg.tools[name] = spec
    cfg.watch = list(cfg.watch) + list(getattr(args, "watch", None) or [])
    cfg.validate()
    return cfg


def connect(cfg: ProxyConfig) -> Tuple[WitnessClient, Optional[EmbeddedWitness]]:
    """A remote witness if one is named, otherwise a witness that runs inside the proxy and saves to the data folder."""
    if cfg.witness_url:
        return WitnessClient(cfg.witness_url, cfg.api_key, cfg.witness_timeout), None
    embedded = EmbeddedWitness(os.path.join(cfg.data_dir, "witness"), dev=cfg.dev_mandate is not None,
                               principal_keys=[cfg.principal_key] if cfg.principal_key else None, timeout=cfg.witness_timeout)
    return embedded.client, embedded


def prepare(cfg: ProxyConfig) -> Tuple[WitnessClient, Optional[EmbeddedWitness], MandateInfo]:
    client, embedded = connect(cfg)
    try:
        info = open_mandate(client, file=cfg.mandate_file, mandate_id=cfg.mandate_id, dev=cfg.dev_mandate, principal_key=cfg.principal_key)
        if cfg.witness_key and info.witness_pub != cfg.witness_key:
            raise WitnessError("the witness's key is not the one you pinned, so it is not the witness you meant to use")
    except Exception:
        if embedded:
            embedded.stop()
        raise
    return client, embedded, info


def _banner(info: MandateInfo, cfg: ProxyConfig, where: str) -> None:
    m = info.mandate
    say(f"mandate {m.mandate_id}: {m.intent!r}; witness key {info.witness_pub[:16]}...; {info.receipts} receipt(s) already recorded, {info.spent:.2f} spent")
    say(f"witnessing {', '.join(sorted(cfg.tools) + cfg.watch)}; enforcement {cfg.enforce}; {where}")


def _start(args: argparse.Namespace) -> Tuple[ProxyConfig, Engine, Optional[EmbeddedWitness], MandateInfo]:
    cfg = build_config(args)
    client, embedded, info = prepare(cfg)
    audit = AuditLog(os.path.join(cfg.data_dir, "audit.jsonl"))
    try:
        engine = Engine(cfg, client, info, audit)
    except Exception:
        audit.close()
        if embedded:
            embedded.stop()
        raise
    audit.event("started", mandate_id=info.mandate.mandate_id, witness_pub=info.witness_pub, enforce=cfg.enforce, tools=sorted(cfg.tools) + cfg.watch)
    engine.start()
    return cfg, engine, embedded, info


def cmd_run(args: argparse.Namespace) -> int:
    from .stdio import run_stdio
    upstream = [a for a in args.upstream if a != "--"] if args.upstream and args.upstream[0] == "--" else list(args.upstream)
    try:
        cfg, engine, embedded, info = _start(args)
    except (ConfigError, WitnessError) as exc:
        say(f"cannot start: {exc}")
        return 2
    command = upstream or cfg.upstream.command
    if not command:
        say("cannot start: no tool server to run. Put its command after --, for example: evenhand-mcp run ... -- npx some-mcp-server")
        engine.close()
        if embedded:
            embedded.stop()
        return 2
    _banner(info, cfg, f"saving to {os.path.abspath(cfg.data_dir)}")
    try:
        return run_stdio(engine, command, env=cfg.upstream.env, cwd=cfg.upstream.cwd)
    finally:
        if embedded:
            embedded.stop()


def cmd_serve_http(args: argparse.Namespace) -> int:
    from .httpproxy import Gateway
    engine: Optional[Engine] = None
    embedded: Optional[EmbeddedWitness] = None
    try:
        cfg, engine, embedded, info = _start(args)
        host, _, port = args.listen.rpartition(":")
        headers = {}
        for h in args.header or []:
            name, _, value = h.partition(":")
            if not value:
                raise ConfigError(f"--header {h!r}: write it as 'Name: value'")
            headers[name.strip()] = value.strip()
        gateway = Gateway(engine, args.upstream_url, host or "127.0.0.1", int(port), headers, args.allow_origin or [], args.upstream_timeout, args.allow_host or [])
    except (ConfigError, WitnessError, ValueError, OSError) as exc:
        say(f"cannot start: {exc}")
        if engine is not None:
            engine.close()
        if embedded is not None:
            embedded.stop()
        return 2
    h, p = gateway.address
    _banner(info, cfg, f"saving to {os.path.abspath(cfg.data_dir)}")
    say(f"gateway listening on http://{h}:{p}{gateway.path}, passing everything to {args.upstream_url}")
    if h not in ("127.0.0.1", "localhost", "::1"):
        say("WARNING: this gateway has no authentication of its own. Do not expose it beyond a network you trust.")
    try:
        gateway.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        gateway.stop()
        engine.close()
        if embedded:
            embedded.stop()
    return 0


# ----- the report -----
def _last_started(data_dir: str) -> Optional[str]:
    started = [r for r in AuditLog.read(os.path.join(data_dir, "audit.jsonl")) if r.get("event") == "started"]
    return started[-1].get("mandate_id") if started else None


def cmd_report(args: argparse.Namespace) -> int:
    embedded: Optional[EmbeddedWitness] = None
    try:
        if args.witness_url:
            client = WitnessClient(args.witness_url, args.api_key)
        else:
            embedded = EmbeddedWitness(os.path.join(args.data_dir, "witness"))
            client = embedded.client
        mandate_id = args.mandate_id or _last_started(args.data_dir)
        if not mandate_id:
            raise ConfigError("which mandate? give --mandate-id (the proxy has not recorded one in this folder yet)")
        return _report(client, mandate_id, args, embedded.witness_pub if embedded else None)
    except (ConfigError, WitnessError) as exc:
        print(f"cannot make the report: {exc}", file=sys.stderr)
        return 2
    finally:
        if embedded:
            embedded.stop()


def _dispute_seqs(text: str, count: int) -> List[int]:
    if text == "all":
        return list(range(count))
    if text in ("none", ""):
        return []
    try:
        seqs = [int(part) for part in _csv(text) or []]
    except ValueError:
        raise ConfigError('--disputes wants "all", "none", or receipt numbers such as 1,3')
    if any(n < 0 or n >= count for n in seqs):
        raise ConfigError(f"--disputes names a receipt that does not exist (there are {count}, numbered from 0)")
    return seqs


def _report(client: WitnessClient, mandate_id: str, args: argparse.Namespace, own_key: Optional[str]) -> int:
    bundle = client.chain(mandate_id)
    m = bundle["mandate"]["mandate"]
    receipts = bundle["receipts"]
    print(f"Mandate {m['mandate_id']}: {m['intent']!r}")
    c = m["constraints"]
    limits = [f"{k.replace('_', ' ')} {v}" for k, v in (("max_per_txn", c["max_per_txn"]), ("max_total", c["max_total"]), ("step_up_above", c["step_up_above"])) if v is not None]
    print("  limits: " + (", ".join(limits) or "none") + "; merchants: " + (", ".join(c["merchants_allow"]) if c["merchants_allow"] is not None else "any")
          + "; categories: " + (", ".join(c["categories_allow"]) if c["categories_allow"] is not None else "any"))
    print(f"\n  {'seq':>3}  {'time':<19}  {'merchant':<14} {'amount':>9}  description")
    for r in receipts:
        a = r["action"]
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(a["ts"]))
        print(f"  {r['seq']:>3}  {when:<19}  {a['merchant_id']:<14} {a['amount']:>9.2f}  {a['cart'][0]['desc'] if a['cart'] else ''}")
    print(f"  {len(receipts)} receipt(s), {sum(r['action']['amount'] for r in receipts):.2f} in total\n")

    witness_key = args.witness_key or own_key or bundle["witness_pub"]
    pinned = bool(args.witness_key and args.user_key)
    result = verify_bundle(bundle, expected_principal_pub=args.user_key, expected_witness_pub=witness_key)
    exit_code = 0
    if args.checkpoint_file:
        try:
            with open(args.checkpoint_file, "r", encoding="utf-8") as f:
                saved = json.load(f)
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read the checkpoint file {args.checkpoint_file}: {exc}")
        result = verify_bundle_with_checkpoint(bundle, saved, expected_witness_pub=witness_key, expected_principal_pub=args.user_key)
        print(f"Checked against the checkpoint in {args.checkpoint_file} (it covered {saved.get('count')} receipts): {'ok' if result['ok'] else 'FAILED'}")
    print("Verification: " + ("the chain is intact" if result["ok"] else "FAILED"))
    for problem in result["problems"]:
        print(f"  - {problem}")
    if not result["ok"]:
        exit_code = 1
    if not pinned and not args.checkpoint_file:
        print("  Note: without --witness-key and --user-key from sources other than this witness, this proves only that the record is consistent with itself.")
    if args.save_checkpoint and receipts:
        cp = client.checkpoint(mandate_id)
        with open(args.save_checkpoint, "w", encoding="utf-8") as f:
            json.dump(cp, f, indent=2)
        print(f"Saved a signed checkpoint ({cp['count']} receipts) to {args.save_checkpoint}. Keep it somewhere the witness cannot reach.")
    seqs = _dispute_seqs(args.disputes, len(receipts))
    if seqs:
        print("\nVerdicts:")
        packets_dir = args.packets
        if packets_dir:
            os.makedirs(packets_dir, exist_ok=True)
        try:
            from ..report import render_packet_pdf
        except ImportError:
            render_packet_pdf = None  # type: ignore[assignment]
        for seq in seqs:
            dispute = client.dispute(mandate_id, seq)
            verdict = dispute["verdict"]
            a = receipts[seq]["action"]
            print(f"  receipt {seq} ({a['merchant_id']}, {a['amount']:.2f}): {verdict['type']}")
            for reason in verdict["reasons"]:
                print(f"      {reason}")
            if packets_dir:
                packet = client.packet(dispute["dispute_id"])
                base = os.path.join(packets_dir, f"{dispute['dispute_id']}-{verdict['type']}")
                with open(base + ".json", "w", encoding="utf-8") as f:
                    json.dump(packet, f, indent=2)
                if render_packet_pdf is not None:
                    try:
                        render_packet_pdf(packet, base + ".pdf")
                    except Exception as exc:  # a missing optional dependency must not spoil the report
                        print(f"      (no PDF: {exc})")
        if packets_dir:
            print(f"  Evidence packets are in {packets_dir}" + ("" if render_packet_pdf else " (install reportlab for PDF versions)"))
    if args.data_dir:
        events = AuditLog.read(os.path.join(args.data_dir, "audit.jsonl"))
        counts: Dict[str, int] = {}
        for e in events:
            counts[e["event"]] = counts.get(e["event"], 0) + 1
        notable = {k: v for k, v in counts.items() if k in ("blocked", "queued", "unrecorded", "interrupted", "tool_error", "error", "input_required")}
        if notable:
            print("\nThe proxy's own log also shows: " + ", ".join(f"{v} {k}" for k, v in sorted(notable.items())))
    return exit_code


# ----- the command line itself -----
def _setup_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="a JSON settings file; flags override it")
    p.add_argument("--data-dir", help="where the witness key, its records and the proxy's own log are kept (default ~/.evenhand-mcp). Use a full path, and a folder of its own for each proxy")
    p.add_argument("--witness-url", help="use a witness server that is already running, instead of one inside the proxy")
    p.add_argument("--witness-key", help="the witness's public key, to pin")
    p.add_argument("--api-key", help="the witness server's bearer token")
    p.add_argument("--mandate", help="a signed mandate file (see: mandate create)")
    p.add_argument("--mandate-id", help="a mandate the witness already has")
    p.add_argument("--dev-mandate", metavar="INTENT", help="make a throwaway mandate for trying things out (testing only)")
    p.add_argument("--merchants", help="for --dev-mandate: allowed merchants, comma separated")
    p.add_argument("--categories", help="for --dev-mandate: allowed categories, comma separated")
    p.add_argument("--max-per-txn", type=float, help="for --dev-mandate")
    p.add_argument("--max-total", type=float, help="for --dev-mandate")
    p.add_argument("--hours", type=float, help="for --dev-mandate: how long it lasts")
    p.add_argument("--principal-key", help="accept mandates only from this user key")
    p.add_argument("--tool", action="append", metavar="NAME:merchant=PATH,amount=PATH,...", help="a tool to witness, and where its merchant and amount are")
    p.add_argument("--watch", action="append", metavar="PATTERN", help="witness tools matching this pattern, guessing where the merchant and amount are")
    p.add_argument("--enforce", choices=["off", "hard", "strict"], help="block purchases outside the mandate before they happen")
    p.add_argument("--on-witness-down", choices=["block", "queue"], help="what to do when the witness cannot be reached")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="evenhand-mcp", description="An MCP witness proxy: records every purchase an agent makes through its tools.")
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="witness a local MCP server (stdio)")
    _setup_args(run)
    run.add_argument("upstream", nargs=argparse.REMAINDER, help="-- followed by the real server's command")
    run.set_defaults(func=cmd_run)

    http = sub.add_parser("serve-http", help="witness a remote MCP server (Streamable HTTP) through a local gateway")
    _setup_args(http)
    http.add_argument("--upstream-url", required=True)
    http.add_argument("--listen", default="127.0.0.1:8788", help="HOST:PORT for the gateway")
    http.add_argument("--header", action="append", metavar="'Name: value'", help="a header to send to the server on every request")
    http.add_argument("--allow-origin", action="append", help="a browser origin allowed to use the gateway (local pages always are)")
    http.add_argument("--allow-host", action="append", help="a host name the gateway answers to besides localhost and its own address")
    http.add_argument("--upstream-timeout", type=float, default=300.0)
    http.set_defaults(func=cmd_serve_http)

    keygen = sub.add_parser("keygen", help="make the user's signing key")
    keygen.add_argument("--out", required=True)
    keygen.set_defaults(func=cmd_keygen)

    mandate = sub.add_parser("mandate", help="work with mandates").add_subparsers(dest="mandate_command", required=True)
    create = mandate.add_parser("create", help="sign a mandate to a file")
    create.add_argument("--key", required=True, help="the user's key file (see: keygen)")
    create.add_argument("--intent", required=True)
    create.add_argument("--merchants")
    create.add_argument("--categories")
    create.add_argument("--max-per-txn", type=float)
    create.add_argument("--max-total", type=float)
    create.add_argument("--step-up-above", type=float)
    create.add_argument("--hours", type=float, default=24.0)
    create.add_argument("--agent-id", default="agent-1")
    create.add_argument("--user-id", default="user-1")
    create.add_argument("--mandate-id", default=f"m-{int(time.time())}")
    create.add_argument("--out", required=True)
    create.set_defaults(func=cmd_mandate_create)

    report = sub.add_parser("report", help="see what was recorded, verify it, and judge disputes")
    report.add_argument("--data-dir", default=DEFAULT_DATA_DIR)
    report.add_argument("--witness-url")
    report.add_argument("--api-key")
    report.add_argument("--mandate-id")
    report.add_argument("--witness-key", help="the witness's public key, from a source other than the witness")
    report.add_argument("--user-key", help="the user's public key, from the user")
    report.add_argument("--checkpoint-file", help="check the chain against a checkpoint saved earlier")
    report.add_argument("--save-checkpoint", metavar="FILE", help="ask the witness for a signed checkpoint and save it")
    report.add_argument("--disputes", default="none", help='"all", "none", or receipt numbers such as 1,3')
    report.add_argument("--packets", metavar="DIR", help="write an evidence packet for each dispute here")
    report.set_defaults(func=cmd_report)

    selftest = sub.add_parser("selftest", help="check that the proxy works on this machine")
    selftest.set_defaults(func=lambda a: __import__("evenhand.mcp.demo", fromlist=["selftest"]).selftest())
    demo = sub.add_parser("demo", help="watch the whole story: an agent buys, one purchase is out of bounds, the evidence is checked")
    demo.add_argument("--keep", action="store_true", help="keep the files it makes")
    demo.add_argument("--dir", help="where to put them")
    demo.set_defaults(func=lambda a: __import__("evenhand.mcp.demo", fromlist=["demo"]).demo(a.dir, a.keep))
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def entry() -> None:
    """The console script. It leaves with os._exit, because a thread still waiting on the agent host's input must not hold the process open."""
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
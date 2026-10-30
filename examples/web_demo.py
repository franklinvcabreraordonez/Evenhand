"""Evenhand browser demo. Runs the real engine on your computer and shows what it does.

Run from the repository root:  python examples/web_demo.py
It prints an address and tries to open your browser. Press Ctrl+C to stop.
Synthetic data only. Nothing leaves your computer.
"""
import argparse
import dataclasses
import json
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # works without installing

from evenhand.adjudicator import adjudicate
from evenhand.crypto import Signer, canonical, verify
from evenhand.evidence import build_packet
from evenhand.mandate import Constraints, Mandate, sign_mandate, sign_revocation, verify_mandate
from evenhand.receipts import AgentAction, CartItem, Witness

BASE = {"mandate": {"merchants": "stridemart", "max_per_txn": "120", "max_total": "300", "intent": "buy running shoes under $120"},
        "action": {"merchant": "stridemart", "amount": "99", "desc": "road running shoes"}, "options": {}}


def preset(name, expect, mandate=None, action=None, options=None):
    cfg = {k: dict(v) for k, v in BASE.items()}
    cfg["mandate"].update(mandate or {}); cfg["action"].update(action or {}); cfg["options"].update(options or {})
    return {"name": name, "expect": expect, "cfg": cfg}


PRESETS = [
    preset("Agent buys shoes at an allowed merchant for $99", "within_mandate"),
    preset("Same, but at a merchant not on the user's list", "exceeded_mandate", action={"merchant": "shadymart", "amount": "89", "desc": "running shoes"}),
    preset("A $150 purchase against a $120 limit", "exceeded_mandate", action={"amount": "150"}),
    preset("Agent buys a laptop bag when told to buy shoes", "exceeded_mandate", action={"desc": "laptop bag", "amount": "40"}),
    preset("Trail shoes bought when road shoes were requested", "ambiguous_intent", mandate={"intent": "buy road running shoes under $120"}, action={"desc": "trail running shoes"}),
    preset("The user revoked the mandate before the purchase", "exceeded_mandate", options={"revoke": True}),
    preset("Someone edits a receipt after the fact", "evidence_invalid", action={"merchant": "shadymart", "amount": "89", "desc": "running shoes"}, options={"edit_receipt": True}),
    preset("A receipt is deleted from the log", "evidence_invalid", action={"merchant": "shadymart", "amount": "89", "desc": "running shoes"}, options={"delete_earlier": True}),
    preset("The disputed receipt is never supplied", "evidence_insufficient", options={"omit_receipt": True}),
    preset("A mandate signed by someone other than the user", "evidence_invalid", options={"forge_mandate": True}),
]


def _num(x, default):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return max(0.0, v) if v == v and abs(v) < 1e9 else default


def run_case(cfg):
    now = time.time()
    m, a, o = cfg.get("mandate", {}), cfg.get("action", {}), cfg.get("options", {})
    allowed = tuple(x.strip() for x in str(m.get("merchants", "")).split(",") if x.strip()) or None
    cap, total = _num(m.get("max_per_txn"), 120.0), _num(m.get("max_total"), 300.0)
    intent = str(m.get("intent") or BASE["mandate"]["intent"])[:200]
    merchant, desc = str(a.get("merchant") or "stridemart")[:60], str(a.get("desc") or "road running shoes")[:100]
    amount = _num(a.get("amount"), 99.0)

    principal, witness = Signer(), Witness()
    mandate = Mandate("m-001", "user-42", "shopping-agent-1", now - 3600, now + 86_400, ("purchase",),
                      Constraints(max_per_txn=cap, max_total=total, merchants_allow=allowed, categories_allow=("sports",)), intent)
    signed = sign_mandate(mandate, principal)
    presented = sign_mandate(mandate, Signer()) if o.get("forge_mandate") else signed

    def act(mer, d, amt):
        return witness.record(AgentAction(mandate.mandate_id, mandate.agent_id, "purchase", amt, "USD", mer, "sports",
                                          (CartItem("sku-1", d, amt),), True, now))

    legit = (allowed or ("stridemart",))[0]
    if o.get("delete_earlier") or o.get("omit_receipt"):
        act(legit, "road running shoes", 50.0)
    target = act(merchant, desc, amount)
    chain = list(witness.chain(mandate.mandate_id))
    if o.get("edit_receipt"):
        i = target.seq
        chain[i] = dataclasses.replace(chain[i], action=dataclasses.replace(chain[i].action, merchant_id=legit))
    if o.get("delete_earlier"):
        chain = chain[1:]
    if o.get("omit_receipt"):
        chain = chain[:-1]
    revocations = [sign_revocation(mandate.mandate_id, now - 1800, principal)] if o.get("revoke") else []

    verdict = adjudicate(presented, chain, target.seq, witness.public_hex, expected_principal_pub=principal.public_hex, revocations=revocations)
    packet = build_packet("case-001", presented, chain, target.seq, verdict, witness.public_hex)
    genuine = presented.principal_pub == principal.public_hex and verify_mandate(presented)
    note = f"Signed with the user's key ({presented.principal_pub[:8]}...)" if genuine else f"Signed with a different key ({presented.principal_pub[:8]}...): forged"
    if o.get("revoke"):
        note += ". The user revoked it 30 minutes before the purchase."
    return {
        "mandate": {"merchants": ", ".join(allowed) if allowed else "any", "cap": f"{cap:g}", "total": f"{total:g}", "intent": intent,
                    "signature_ok": genuine, "signature_note": note},
        "receipts": [{"seq": r.seq, "merchant": r.action.merchant_id, "amount": f"{r.action.amount:g}", "desc": r.action.cart[0].desc,
                      "hash": r.hash[:10], "prev": r.prev_hash[:10],
                      "sig_ok": verify(witness.public_hex, canonical(r.body()), r.signature)} for r in chain],
        "verdict": {"type": verdict.type.value, "reasons": verdict.reasons, "recommendation": verdict.recommendation, "intent_score": verdict.intent_score},
        "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in verdict.checks],
        "packet": packet,
    }


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Evenhand demo</title>
<style>
:root{color-scheme:light;--paper:#E9EDF1;--surface:#fff;--ink:#13202B;--muted:#52616E;--rule:#CFD7DF;--accent:#0B6573;--accent-soft:#DAEEF1;--good:#1B7448;--good-soft:#DCF2E5;--warn:#855300;--warn-soft:#FFEFC2;--bad:#B0261F;--bad-soft:#FADBD8;--serif:"Iowan Old Style","Palatino Linotype",Palatino,"Book Antiqua",Georgia,serif;--sans:"Segoe UI",system-ui,-apple-system,"Helvetica Neue",Arial,sans-serif}
@media(prefers-color-scheme:dark){:root{color-scheme:dark;--paper:#0C131A;--surface:#141E28;--ink:#E4ECF2;--muted:#9AAAB8;--rule:#273747;--accent:#63C4D1;--accent-soft:#12303A;--good:#62CC92;--good-soft:#143324;--warn:#E8BC55;--warn-soft:#3A2E0F;--bad:#F28D86;--bad-soft:#3C1916}}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:16px/1.55 var(--sans)}
header{background:var(--surface);border-bottom:1px solid var(--rule);padding:14px 20px}
header h1{margin:0;font:600 28px/1.1 var(--serif)}header p{margin:4px 0 0;color:var(--muted);font-size:14px;max-width:80ch}
main{max-width:1300px;margin:0 auto;padding:18px;display:grid;grid-template-columns:minmax(300px,390px) minmax(0,1fr);gap:18px;align-items:start}
.card{background:var(--surface);border:1px solid var(--rule);border-radius:6px;padding:16px 18px;margin-bottom:14px}
.card h2{margin:0 0 8px;font:600 19px/1.25 var(--serif)}.card h3{margin:14px 0 6px;font:600 15px var(--sans)}
.muted{color:var(--muted);font-size:14px}.ok{color:var(--good);font-weight:600}.bad{color:var(--bad);font-weight:600}
button{font:inherit;cursor:pointer}
.preset{display:block;width:100%;text-align:left;margin:0 0 6px;padding:8px 10px;border:1px solid var(--rule);border-left:3px solid var(--accent);border-radius:6px;background:var(--paper);color:var(--ink)}
.preset:hover,.preset.on{background:var(--accent-soft)}
label{display:block;font-size:13.5px;font-weight:600;margin:8px 0 3px}
input[type=text],input[type=number]{width:100%;padding:7px 8px;border:1px solid var(--rule);border-radius:5px;background:var(--surface);color:var(--ink);font:inherit}
.chk{display:flex;gap:8px;align-items:flex-start;font-weight:400;margin:6px 0}.chk input{margin-top:4px;accent-color:var(--accent)}
#run{margin-top:12px;padding:9px 16px;border:1px solid var(--accent);background:var(--accent);color:var(--surface);border-radius:6px;font-weight:600}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.tw{overflow-x:auto;border:1px solid var(--rule);border-radius:6px;margin:8px 0}
table{border-collapse:collapse;width:100%;font-size:14px}th,td{padding:7px 10px;border-bottom:1px solid var(--rule);text-align:left;vertical-align:top}thead th{background:var(--paper)}tbody tr:last-child td{border-bottom:0}
tr.badrow td{background:var(--bad-soft)}code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px}
dl.kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:0}dl.kv dt{color:var(--muted)}dl.kv dd{margin:0}
.badge{display:inline-block;padding:6px 16px;border-radius:999px;font-weight:650;font-size:18px;margin:2px 0 8px}
.v-within{background:var(--good-soft);color:var(--good)}.v-exceeded,.v-invalid{background:var(--bad-soft);color:var(--bad)}.v-ambiguous,.v-insufficient{background:var(--warn-soft);color:var(--warn)}
details summary{cursor:pointer;font-weight:600}pre{background:var(--paper);border:1px solid var(--rule);border-radius:6px;padding:12px;overflow:auto;font-size:12.5px;max-height:420px}
@media(max-width:900px){main{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Evenhand</h1><p>Prototype demo. This page runs the real engine on your computer, with made-up data. Pick a situation on the left and watch what Evenhand records and decides. Nothing leaves your machine.</p></header>
<main>
<section>
 <div class="card"><h2>Pick a situation</h2><div id="presets"></div></div>
 <div class="card"><h2>Or build your own</h2>
  <h3>1. What the user signs</h3>
  <label for="f-merchants">Allowed merchants (comma separated)</label><input type="text" id="f-merchants">
  <label for="f-cap">Limit per purchase ($)</label><input type="number" id="f-cap" min="0">
  <label for="f-total">Limit in total ($)</label><input type="number" id="f-total" min="0">
  <label for="f-intent">What the user asked for</label><input type="text" id="f-intent">
  <h3>2. What the agent does</h3>
  <label for="a-merchant">Merchant</label><input type="text" id="a-merchant">
  <label for="a-amount">Amount ($)</label><input type="number" id="a-amount" min="0">
  <label for="a-desc">Item bought</label><input type="text" id="a-desc">
  <h3>3. What goes wrong</h3>
  <label class="chk"><input type="checkbox" id="o-revoke">The user revoked the mandate before the purchase</label>
  <label class="chk"><input type="checkbox" id="o-edit">Someone edits the receipt afterwards</label>
  <label class="chk"><input type="checkbox" id="o-delete">An earlier receipt is deleted from the log</label>
  <label class="chk"><input type="checkbox" id="o-omit">The disputed receipt is never supplied</label>
  <label class="chk"><input type="checkbox" id="o-forge">The mandate was signed by someone else</label>
  <button id="run">Run it</button>
 </div>
</section>
<section>
 <div class="card"><h2>1. The user signs a mandate</h2><p class="muted">The permission slip: what the agent may buy, where, and how much.</p><div id="o-mandate"></div></div>
 <div class="card"><h2>2. The agent acts and a neutral witness records it</h2><p class="muted">Each receipt is signed and carries the fingerprint of the one before it, so editing, deleting or reordering any receipt is detectable.</p><div id="o-receipts"></div></div>
 <div class="card"><h2>3. A dispute arrives: the verdict</h2><div id="o-verdict"></div></div>
 <div class="card"><h2>4. Every check Evenhand ran</h2><div id="o-checks"></div></div>
 <div class="card"><h2>5. The evidence packet a merchant or bank would receive</h2><details><summary>Show the machine-readable packet</summary><pre id="o-packet"></pre></details></div>
</section>
</main>
<script>
const PRESETS = __PRESETS__;
const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const VERDICT = {
  within_mandate: ['Within mandate', 'v-within', 'Everything the user signed was respected. A merchant can contest the dispute with this packet.'],
  exceeded_mandate: ['Exceeded mandate', 'v-exceeded', 'The agent broke a rule the user set, so the dispute is valid and liability is routed per the program terms.'],
  ambiguous_intent: ['Ambiguous intent', 'v-ambiguous', 'Every hard rule passed, but it is unclear whether the agent bought what the user wanted. A human decides.'],
  evidence_insufficient: ['Evidence insufficient', 'v-insufficient', 'The record needed to decide was not supplied. Ask for it.'],
  evidence_invalid: ['Evidence invalid', 'v-invalid', 'The record or the mandate cannot be trusted: it was edited, forged or cut. Treat it as possible fraud.']
};
const FIELDS = {'f-merchants':['mandate','merchants'],'f-cap':['mandate','max_per_txn'],'f-total':['mandate','max_total'],'f-intent':['mandate','intent'],'a-merchant':['action','merchant'],'a-amount':['action','amount'],'a-desc':['action','desc']};
const OPTS = {'o-revoke':'revoke','o-edit':'edit_receipt','o-delete':'delete_earlier','o-omit':'omit_receipt','o-forge':'forge_mandate'};
function fill(cfg){ for (const id in FIELDS) $(id).value = cfg[FIELDS[id][0]][FIELDS[id][1]]; for (const id in OPTS) $(id).checked = !!cfg.options[OPTS[id]]; }
function read(){ const cfg = {mandate:{}, action:{}, options:{}}; for (const id in FIELDS) cfg[FIELDS[id][0]][FIELDS[id][1]] = $(id).value; for (const id in OPTS) cfg.options[OPTS[id]] = $(id).checked; return cfg; }
function render(d){
  const m = d.mandate;
  $('o-mandate').innerHTML = `<dl class="kv"><dt>Allowed merchants</dt><dd>${esc(m.merchants)}</dd><dt>Limit per purchase</dt><dd>$${esc(m.cap)}</dd><dt>Limit in total</dt><dd>$${esc(m.total)}</dd><dt>Valid for</dt><dd>24 hours</dd><dt>What the user asked for</dt><dd>"${esc(m.intent)}"</dd><dt>Signature</dt><dd class="${m.signature_ok ? 'ok' : 'bad'}">${esc(m.signature_note)}</dd></dl>`;
  $('o-receipts').innerHTML = d.receipts.length ? `<div class="tw"><table><thead><tr><th>#</th><th>Merchant</th><th>Amount</th><th>Item</th><th>Fingerprint</th><th>Links back to</th><th>Signature</th></tr></thead><tbody>${d.receipts.map(r => `<tr class="${r.sig_ok ? '' : 'badrow'}"><td>${r.seq}</td><td>${esc(r.merchant)}</td><td>$${esc(r.amount)}</td><td>${esc(r.desc)}</td><td><code>${esc(r.hash)}</code></td><td><code>${esc(r.prev)}</code></td><td class="${r.sig_ok ? 'ok' : 'bad'}">${r.sig_ok ? 'valid' : 'does not verify'}</td></tr>`).join('')}</tbody></table></div>` : '<p class="muted">No receipts were supplied.</p>';
  const v = d.verdict, info = VERDICT[v.type];
  $('o-verdict').innerHTML = `<div class="badge ${info[1]}">${info[0]}</div><p>${esc(info[2])}</p><ul>${v.reasons.map(r => `<li>${esc(r)}</li>`).join('')}</ul><p><strong>Recommended action:</strong> ${esc(v.recommendation)}</p>` + (v.intent_score !== null ? `<p class="muted">Intent match score: ${v.intent_score.toFixed(2)} (0.75 or above matches, 0.25 or below does not)</p>` : '');
  $('o-checks').innerHTML = `<div class="tw"><table><thead><tr><th>Result</th><th>Check</th><th>Detail</th></tr></thead><tbody>${d.checks.map(c => `<tr><td class="${c.passed ? 'ok' : 'bad'}">${c.passed ? 'Pass' : 'Fail'}</td><td>${esc(c.name.replace(/_/g, ' '))}</td><td>${esc(c.detail)}</td></tr>`).join('')}</tbody></table></div>`;
  $('o-packet').textContent = JSON.stringify(d.packet, null, 2);
}
async function go(){
  const res = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(read())});
  const data = await res.json();
  if (data.error) { $('o-verdict').innerHTML = '<p class="bad">' + esc(data.error) + '</p>'; return; }
  render(data);
}
PRESETS.forEach((p, i) => { const b = document.createElement('button'); b.className = 'preset'; b.textContent = p.name; b.onclick = () => { document.querySelectorAll('.preset').forEach(x => x.classList.remove('on')); b.classList.add('on'); fill(p.cfg); go(); }; $('presets').appendChild(b); });
$('run').onclick = () => { document.querySelectorAll('.preset').forEach(x => x.classList.remove('on')); go(); };
document.querySelector('.preset').click();
</script></body></html>"""
PAGE = PAGE.replace("__PRESETS__", json.dumps(PRESETS))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        else:
            self._send(404, "Not found", "text/plain")

    def do_POST(self):
        if self.path != "/api/run":
            return self._send(404, "Not found", "text/plain")
        try:
            n = int(self.headers.get("Content-Length", 0))
            cfg = json.loads(self.rfile.read(min(n, 100_000)) or b"{}")
            self._send(200, json.dumps(run_case(cfg)), "application/json")
        except Exception as exc:  # report bad input instead of crashing the demo
            self._send(400, json.dumps({"error": str(exc)}), "application/json")


def selftest():
    bad = 0
    for p in PRESETS:
        got = run_case(p["cfg"])["verdict"]["type"]
        ok = got == p["expect"]
        bad += not ok
        print(f"{'ok  ' if ok else 'FAIL'} {p['name']}: {got}")
    print(f"{len(PRESETS) - bad}/{len(PRESETS)} situations produce the expected verdict")
    return bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--selftest", action="store_true", help="run every situation without starting the server")
    args = ap.parse_args()
    if args.selftest:
        sys.exit(1 if selftest() else 0)
    server = None
    for port in range(args.port, args.port + 11):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        sys.exit("Could not find a free port. Try: python examples/web_demo.py --port 9000")
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"Evenhand demo running at {url}  (press Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

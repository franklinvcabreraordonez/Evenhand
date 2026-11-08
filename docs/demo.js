/* Evenhand live demo.
   A JavaScript port of the Python engine (mandates, witness receipts, adjudicator,
   evidence packets). It runs entirely in your browser on made-up data. The Python
   code in the repository is the reference implementation. */
(function () {
  'use strict';

  const GENESIS = '0'.repeat(64);
  const enc = new TextEncoder();

  /* ---------- bytes, hex, canonical JSON ---------- */
  const hex = (buf) => Array.from(new Uint8Array(buf), (b) => b.toString(16).padStart(2, '0')).join('');
  const unhex = (h) => {
    const out = new Uint8Array(h.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(h.substr(i * 2, 2), 16);
    return out;
  };
  const concat = (a, b) => {
    const out = new Uint8Array(a.length + b.length);
    out.set(a, 0);
    out.set(b, a.length);
    return out;
  };
  const ascii = (s) => s.replace(/[^\x20-\x7e]/g, (c) => '\\u' + c.charCodeAt(0).toString(16).padStart(4, '0'));

  // Deterministic JSON: sorted keys, no whitespace, ASCII only.
  function canon(v) {
    if (v === null) return 'null';
    if (Array.isArray(v)) return '[' + v.map(canon).join(',') + ']';
    switch (typeof v) {
      case 'string': return ascii(JSON.stringify(v));
      case 'number': return JSON.stringify(v);
      case 'boolean': return v ? 'true' : 'false';
      case 'object':
        return '{' + Object.keys(v).sort().map((k) => ascii(JSON.stringify(k)) + ':' + canon(v[k])).join(',') + '}';
      default: throw new Error('cannot canonicalize ' + typeof v);
    }
  }
  const sha256hex = async (bytes) => hex(await crypto.subtle.digest('SHA-256', bytes));

  /* ---------- number formatting that matches the Python engine ---------- */
  function fmt2(x) { // like Python's f"{x:.2f}" (ties go to the even digit)
    const t = x * 8;
    if (Number.isInteger(t) && t % 2 !== 0) {
      const n = x * 100;
      const lo = Math.floor(n);
      return ((lo % 2 === 0 ? lo : lo + 1) / 100).toFixed(2);
    }
    return x.toFixed(2);
  }
  function fmtG(x) { // like Python's f"{x:g}"
    if (x === 0) return '0';
    const e = Math.floor(Math.log10(Math.abs(x)));
    if (e < -4 || e >= 6) {
      const parts = x.toExponential(5).split('e');
      const n = parseInt(parts[1], 10);
      return parts[0].replace(/\.?0+$/, '') + 'e' + (n < 0 ? '-' : '+') + String(Math.abs(n)).padStart(2, '0');
    }
    return String(Number(x.toPrecision(6)));
  }
  const pyList = (xs) => '[' + xs.map((s) => "'" + s + "'").join(', ') + ']';

  /* ---------- signatures (Web Crypto) ---------- */
  const ED = { label: 'Ed25519', gen: { name: 'Ed25519' }, imp: { name: 'Ed25519' }, sig: { name: 'Ed25519' } };
  const EC = {
    label: 'ECDSA P-256 (this browser has no Ed25519)',
    gen: { name: 'ECDSA', namedCurve: 'P-256' },
    imp: { name: 'ECDSA', namedCurve: 'P-256' },
    sig: { name: 'ECDSA', hash: 'SHA-256' },
  };
  let alg = null;

  async function init(preferEcdsa) {
    alg = EC;
    if (!preferEcdsa) {
      try {
        await crypto.subtle.generateKey(ED.gen, true, ['sign', 'verify']);
        alg = ED;
      } catch (e) { /* fall back to ECDSA */ }
    }
    return alg.label;
  }

  class Signer {
    static async create() {
      const s = new Signer();
      const kp = await crypto.subtle.generateKey(alg.gen, true, ['sign', 'verify']);
      s.priv = kp.privateKey;
      s.publicHex = hex(await crypto.subtle.exportKey('raw', kp.publicKey));
      return s;
    }
    async sign(bytes) { return hex(await crypto.subtle.sign(alg.sig, this.priv, bytes)); }
  }

  async function verify(pubHex, bytes, sigHex) {
    try {
      const key = await crypto.subtle.importKey('raw', unhex(pubHex), alg.imp, false, ['verify']);
      return await crypto.subtle.verify(alg.sig, key, unhex(sigHex), bytes);
    } catch (e) { return false; }
  }

  /* ---------- mandates ---------- */
  const makeConstraints = (o) => Object.assign({
    max_per_txn: null, max_total: null, currency: 'USD', merchants_allow: null,
    merchants_deny: [], categories_allow: null, step_up_above: null,
  }, o);
  const mandateDict = (m) => ({
    mandate_id: m.mandate_id, principal_id: m.principal_id, agent_id: m.agent_id,
    issued_at: m.issued_at, expires_at: m.expires_at, actions: m.actions,
    constraints: m.constraints, intent: m.intent,
  });
  async function signMandate(m, principal) {
    return { mandate: m, principal_pub: principal.publicHex, signature: await principal.sign(enc.encode(canon(mandateDict(m)))) };
  }
  const verifyMandate = (sm) => verify(sm.principal_pub, enc.encode(canon(mandateDict(sm.mandate))), sm.signature);
  const mandateDigest = (sm) => sha256hex(enc.encode(canon(mandateDict(sm.mandate))));

  const revPayload = (id, at) => enc.encode(canon({ mandate_id: id, revoked_at: at, type: 'revocation' }));
  async function signRevocation(id, at, principal) {
    return { mandate_id: id, revoked_at: at, principal_pub: principal.publicHex, signature: await principal.sign(revPayload(id, at)) };
  }
  const verifyRevocation = (r) => verify(r.principal_pub, revPayload(r.mandate_id, r.revoked_at), r.signature);

  /* ---------- witness receipts ---------- */
  const rbody = (r) => ({ seq: r.seq, prev_hash: r.prev_hash, action: r.action, witness_pub: r.witness_pub });
  const receiptHash = (r) => sha256hex(concat(enc.encode(canon(rbody(r))), unhex(r.signature)));

  class Witness {
    constructor(signer) { this.signer = signer; this.chains = new Map(); }
    get publicHex() { return this.signer.publicHex; }
    async record(action) {
      const chain = this.chains.get(action.mandate_id) || [];
      this.chains.set(action.mandate_id, chain);
      const prev = chain.length ? await receiptHash(chain[chain.length - 1]) : GENESIS;
      const body = { seq: chain.length, prev_hash: prev, action: action, witness_pub: this.publicHex };
      const r = Object.assign({}, body, { signature: await this.signer.sign(enc.encode(canon(body))) });
      chain.push(r);
      return r;
    }
    chain(id) { return (this.chains.get(id) || []).slice(); }
  }

  async function verifyChain(receipts, witnessPub, mandateId, expectedHead) {
    const problems = [];
    let prev = GENESIS;
    for (let i = 0; i < receipts.length; i++) {
      const r = receipts[i];
      if (r.witness_pub !== witnessPub) problems.push(`receipt ${r.seq}: signed by an unexpected witness key`);
      if (!(await verify(witnessPub, enc.encode(canon(rbody(r))), r.signature))) problems.push(`receipt ${r.seq}: signature does not verify (content altered or forged)`);
      if (r.seq !== i) problems.push(`position ${i} holds seq ${r.seq} (gap, deletion or reorder)`);
      if (r.prev_hash !== prev) problems.push(`receipt ${r.seq}: broken hash link to previous receipt`);
      if (r.action.mandate_id !== mandateId) problems.push(`receipt ${r.seq}: belongs to a different mandate`);
      prev = await receiptHash(r);
    }
    const head = receipts.length ? prev : null;
    if (expectedHead != null && head !== expectedHead) problems.push('chain head does not match the witness checkpoint (truncated or extended)');
    return { ok: problems.length === 0, problems: problems, head: head };
  }

  /* ---------- intent matching ---------- */
    /* ---------- intent matching ---------- */
  const STOP = new Set(['a', 'an', 'the', 'for', 'my', 'me', 'to', 'of', 'and', 'buy', 'get', 'some', 'new', 'under',
    'around', 'than', 'less', 'cheap', 'affordable', 'best', 'good', 'please', 'with', 'in', 'on']);
  const GENERIC = new Set(['computer', 'device', 'product', 'item', 'set', 'pair', 'kit', 'pack', 'bundle', 'unit']);
  const WORD_SYN = {
    sneaker: 'shoe', trainer: 'shoe', notebook: 'laptop', earbud: 'headphone', headset: 'headphone',
    smartphone: 'phone', cellphone: 'phone', mobile: 'phone',
  };
  // Real retail synonyms. Deliberately small and explicit; a model or a person handles the rest.
  const PHRASE_SYN = {
    'ground coffee': ['coffee', 'bean'], 'exercise mat': ['yoga', 'mat'],
    'desk chair': ['office', 'chair'], 'computer chair': ['office', 'chair'],
  };
  const PARSE = /^buy ([a-z0-9 \-]+?) under \$(\d+(?:\.\d+)?)$/i;
  const SPLIT = /\s*(?:\+|&|,|;|\band\b|\bwith\b|\bplus\b)\s*/;
  const has = (o, k) => Object.prototype.hasOwnProperty.call(o, k);

  function phrases(toks) {
    const out = [];
    for (let i = 0; i < toks.length;) {
      const key = toks.slice(i, i + 2).join(' ');
      if (i + 1 < toks.length && has(PHRASE_SYN, key)) { out.push(...PHRASE_SYN[key]); i += 2; }
      else { out.push(toks[i]); i += 1; }
    }
    return out;
  }

  function normalize(text) {
    let toks = [];
    for (let w of (text.toLowerCase().match(/[a-z]+/g) || [])) {
      if (STOP.has(w)) continue;
      if (w.length > 3 && w.endsWith('s') && !w.endsWith('ss')) w = w.slice(0, -1);
      toks.push(has(WORD_SYN, w) ? WORD_SYN[w] : w);
    }
    toks = phrases(toks);
    if (toks.length > 1) { // "notebook computer" is a laptop
      const kept = toks.filter((t) => !GENERIC.has(t));
      if (kept.length) toks = kept;
    }
    return toks;
  }

  function withinOneEdit(a, b) {
    if (a === b) return true;
    if (Math.abs(a.length - b.length) > 1) return false;
    if (a.length === b.length) {
      const diff = [];
      for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) diff.push(i);
      if (diff.length === 1) return true;
      return diff.length === 2 && diff[1] === diff[0] + 1 && a[diff[0]] === b[diff[1]] && a[diff[1]] === b[diff[0]];
    }
    const short = a.length < b.length ? a : b;
    const long = a.length < b.length ? b : a;
    for (let i = 0; i < long.length; i++) if (long.slice(0, i) + long.slice(i + 1) === short) return true;
    return false;
  }
  // Exact, or one typo apart for words of five letters or more. Near: four letters or more, enough to ask a person.
  const sameWord = (a, b) => a === b || (Math.min(a.length, b.length) >= 5 && withinOneEdit(a, b));
  const nearWord = (a, b) => a === b || (Math.min(a.length, b.length) >= 4 && withinOneEdit(a, b));

  function intentScore(intent, cart) {
    const parsed = PARSE.exec(intent.trim());
    if (!parsed || !cart.length) return 0.5; // cannot parse: escalate, do not guess
    const want = normalize(parsed[1]);
    if (!want.length) return 0.5;
    const wanted = Array.from(new Set(want));
    let best = 0, near = 0;
    for (const it of cart) {
      for (const segment of it.desc.toLowerCase().split(SPLIT)) {
        const have = normalize(segment);
        if (!have.length) continue;
        for (const pair of [[sameWord, true], [nearWord, false]]) {
          const same = pair[0];
          let cov = wanted.filter((w) => have.some((h) => same(w, h))).length / wanted.length;
          if (!same(want[want.length - 1], have[have.length - 1])) cov = Math.min(cov, 0.2); // "laptop bag" is not a "laptop"
          if (pair[1]) best = Math.max(best, cov); else near = Math.max(near, cov);
        }
      }
    }
    if (best <= 0.25 && near >= 0.5) return 0.5; // looks like a typo of the right product: ask a person
    return best;
  }

  /* ---------- adjudication ---------- */
  const RECOMMEND = {
    within_mandate: 'Contest the dispute: the signed mandate authorized this action. Attach the evidence packet.',
    exceeded_mandate: 'Accept the dispute: the agent acted outside its mandate. Route liability per program terms.',
    ambiguous_intent: 'Constraints were met but intent is unclear. Send to human review with the packet.',
    evidence_insufficient: 'Request the missing witness receipts before deciding.',
    evidence_invalid: 'Treat as potential fraud or tampering. Escalate; do not rely on submitted logs.',
  };
  const finish = (type, checks, reasons, score, head) => ({
    type: type, checks: checks, reasons: reasons,
    intent_score: score === undefined ? null : score, recommendation: RECOMMEND[type], chain_head: head === undefined ? null : head,
  });

  async function adjudicate(sm, receipts, targetSeq, witnessPub, opts) {
    opts = opts || {};
    const hi = opts.hi === undefined ? 0.75 : opts.hi;
    const lo = opts.lo === undefined ? 0.25 : opts.lo;
    const m = sm.mandate, c = m.constraints, checks = [];

    // 1. Is the mandate genuine and from the expected principal?
    const sigOk = await verifyMandate(sm);
    const keyOk = opts.expectedPrincipalPub == null || opts.expectedPrincipalPub === sm.principal_pub;
    checks.push({ name: 'mandate_signature', passed: sigOk && keyOk, detail: sigOk && keyOk ? 'verified' : 'signature invalid or signed by an unexpected key' });
    if (!(sigOk && keyOk)) return finish('evidence_invalid', checks, ['mandate cannot be authenticated']);

    // 2. Is the witness log intact?
    const report = await verifyChain(receipts, witnessPub, m.mandate_id, opts.expectedHead);
    checks.push({ name: 'log_integrity', passed: report.ok, detail: report.ok ? 'chain intact' : report.problems.join('; ') });
    if (!report.ok) return finish('evidence_invalid', checks, report.problems, null, report.head);

    // 3. Is the disputed action in the log?
    const target = receipts.find((r) => r.seq === targetSeq);
    if (!target) {
      checks.push({ name: 'target_present', passed: false, detail: `receipt ${targetSeq} not supplied` });
      return finish('evidence_insufficient', checks, ['disputed action not in supplied log'], null, report.head);
    }
    const a = target.action;

    // 4. Hard constraints (all evaluated so the packet lists every violation).
    const failures = [];
    const hard = (name, ok, detail) => { checks.push({ name: name, passed: ok, detail: detail }); if (!ok) failures.push(detail); };
    hard('agent_binding', a.agent_id === m.agent_id && a.mandate_id === m.mandate_id,
      a.agent_id === m.agent_id ? 'action was performed by the authorized agent' : `action by ${a.agent_id}, mandate names ${m.agent_id}`);
    hard('action_type', m.actions.includes(a.action), `action '${a.action}' vs allowed ${pyList(m.actions)}`);
    const inWindow = m.issued_at <= a.ts && a.ts <= m.expires_at;
    hard('time_window', inWindow, inWindow ? 'within validity window' : 'action outside mandate validity window');
    let revoked = false;
    for (const r of (opts.revocations || [])) {
      if ((await verifyRevocation(r)) && r.principal_pub === sm.principal_pub && r.mandate_id === m.mandate_id && r.revoked_at <= a.ts) { revoked = true; break; }
    }
    hard('not_revoked', !revoked, !revoked ? 'mandate not revoked before action' : 'mandate revoked before action');
    if (c.max_per_txn !== null) hard('per_txn_cap', a.amount <= c.max_per_txn, `amount ${fmt2(a.amount)} vs cap ${fmt2(c.max_per_txn)}`);
    if (c.max_total !== null) {
      let prior = 0;
      for (const r of receipts) if (r.seq < target.seq && m.actions.includes(r.action.action)) prior += r.action.amount;
      hard('cumulative_cap', prior + a.amount <= c.max_total, `cumulative ${fmt2(prior + a.amount)} vs cap ${fmt2(c.max_total)}`);
    }
    if (c.merchants_allow !== null) hard('merchant_allowed', c.merchants_allow.includes(a.merchant_id), `merchant ${a.merchant_id}`);
    hard('merchant_not_denied', !c.merchants_deny.includes(a.merchant_id), `merchant ${a.merchant_id}`);
    if (c.categories_allow !== null) hard('category_allowed', c.categories_allow.includes(a.category), `category ${a.category}`);
    if (c.step_up_above !== null && a.amount > c.step_up_above) hard('step_up', a.step_up_done, `amount above ${fmt2(c.step_up_above)} requires human confirmation`);
    if (failures.length) return finish('exceeded_mandate', checks, failures, null, report.head);

    // 5. Intent (the gray zone).
    const score = intentScore(m.intent, a.cart.map((it) => ({ desc: it.desc, price: it.price })));
    if (score >= hi) {
      checks.push({ name: 'intent_match', passed: true, detail: `score ${fmt2(score)}` });
      return finish('within_mandate', checks, ['all constraints met; intent consistent'], score, report.head);
    }
    if (score <= lo) {
      checks.push({ name: 'intent_match', passed: false, detail: `score ${fmt2(score)}` });
      return finish('exceeded_mandate', checks, ['purchase does not match stated intent'], score, report.head);
    }
    checks.push({ name: 'intent_match', passed: false, detail: `score ${fmt2(score)} (ambiguous)` });
    return finish('ambiguous_intent', checks, ['constraints met but intent match is unclear'], score, report.head);
  }

  async function buildPacket(caseId, sm, receipts, targetSeq, verdict, witnessPub) {
    const target = receipts.find((r) => r.seq === targetSeq);
    return {
      case_id: caseId,
      verdict: verdict.type,
      recommendation: verdict.recommendation,
      reasons: verdict.reasons,
      intent_score: verdict.intent_score,
      checks: verdict.checks,
      mandate: {
        id: sm.mandate.mandate_id, digest: await mandateDigest(sm), principal_pub: sm.principal_pub,
        agent_id: sm.mandate.agent_id, intent: sm.mandate.intent, constraints: sm.mandate.constraints,
      },
      witness_pub: witnessPub,
      chain_head: verdict.chain_head,
      disputed_receipt: target ? { seq: target.seq, hash: await receiptHash(target), prev_hash: target.prev_hash, action: target.action } : null,
      receipts_supplied: receipts.length,
    };
  }

  /* ---------- one demo case, end to end ---------- */
  const DECIMAL = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
  function num(x, dflt) {
    const t = String(x == null ? '' : x).trim();
    if (!DECIMAL.test(t)) return dflt;
    const v = Number(t);
    if (!Number.isFinite(v) || Math.abs(v) >= 1e9) return dflt;
    return Math.max(0, v);
  }

  async function runCase(cfg) {
    const now = Date.now() / 1000;
    const m = cfg.mandate || {}, a = cfg.action || {}, o = cfg.options || {};
    const list = String(m.merchants == null ? '' : m.merchants).split(',').map((x) => x.trim()).filter((x) => x);
    const allowed = list.length ? list : null;
    const cap = num(m.max_per_txn, 120), total = num(m.max_total, 300);
    const intent = String(m.intent || 'buy running shoes under $120').slice(0, 200);
    const merchant = String(a.merchant || 'stridemart').slice(0, 60);
    const desc = String(a.desc || 'road running shoes').slice(0, 100);
    const amount = num(a.amount, 99);

    const principal = await Signer.create();
    const witness = new Witness(await Signer.create());
    const mandate = {
      mandate_id: 'm-001', principal_id: 'user-42', agent_id: 'shopping-agent-1',
      issued_at: now - 3600, expires_at: now + 86400, actions: ['purchase'],
      constraints: makeConstraints({ max_per_txn: cap, max_total: total, merchants_allow: allowed, categories_allow: ['sports'] }),
      intent: intent,
    };
    const signed = await signMandate(mandate, principal);
    const presented = o.forge_mandate ? await signMandate(mandate, await Signer.create()) : signed;

    const act = (mer, d, amt) => witness.record({
      mandate_id: mandate.mandate_id, agent_id: mandate.agent_id, action: 'purchase', amount: amt, currency: 'USD',
      merchant_id: mer, category: 'sports', cart: [{ sku: 'sku-1', desc: d, price: amt, qty: 1 }], step_up_done: true, ts: now,
    });
    const legit = (allowed || ['stridemart'])[0];
    if (o.delete_earlier || o.omit_receipt) await act(legit, 'road running shoes', 50);
    const target = await act(merchant, desc, amount);
    let chain = witness.chain(mandate.mandate_id);
    if (o.edit_receipt) {
      const i = target.seq;
      chain[i] = Object.assign({}, chain[i], { action: Object.assign({}, chain[i].action, { merchant_id: legit }) });
    }
    if (o.delete_earlier) chain = chain.slice(1);
    if (o.omit_receipt) chain = chain.slice(0, -1);
    const revocations = o.revoke ? [await signRevocation(mandate.mandate_id, now - 1800, principal)] : [];

    const verdict = await adjudicate(presented, chain, target.seq, witness.publicHex, { expectedPrincipalPub: principal.publicHex, revocations: revocations });
    const packet = await buildPacket('case-001', presented, chain, target.seq, verdict, witness.publicHex);
    const genuine = presented.principal_pub === principal.publicHex && (await verifyMandate(presented));
    let note = genuine
      ? `Signed with the user's key (${presented.principal_pub.slice(0, 8)}...)`
      : `Signed with a different key (${presented.principal_pub.slice(0, 8)}...): forged`;
    if (o.revoke) note += '. The user revoked it 30 minutes before the purchase.';

    const rows = [];
    for (const r of chain) {
      rows.push({
        seq: r.seq, merchant: r.action.merchant_id, amount: fmtG(r.action.amount), desc: r.action.cart[0].desc,
        hash: (await receiptHash(r)).slice(0, 10), prev: r.prev_hash.slice(0, 10),
        sig_ok: await verify(witness.publicHex, enc.encode(canon(rbody(r))), r.signature),
      });
    }
    return {
      mandate: { merchants: allowed ? allowed.join(', ') : 'any', cap: fmtG(cap), total: fmtG(total), intent: intent, signature_ok: genuine, signature_note: note },
      receipts: rows,
      verdict: { type: verdict.type, reasons: verdict.reasons, recommendation: verdict.recommendation, intent_score: verdict.intent_score },
      checks: verdict.checks,
      packet: packet,
    };
  }

  /* ---------- presets (the same ten situations as the Python demo) ---------- */
  const BASE = {
    mandate: { merchants: 'stridemart', max_per_txn: '120', max_total: '300', intent: 'buy running shoes under $120' },
    action: { merchant: 'stridemart', amount: '99', desc: 'road running shoes' },
    options: {},
  };
  function preset(name, expect, mandate, action, options) {
    return {
      name: name, expect: expect,
      cfg: { mandate: Object.assign({}, BASE.mandate, mandate), action: Object.assign({}, BASE.action, action), options: Object.assign({}, BASE.options, options) },
    };
  }
  const SHADY = { merchant: 'shadymart', amount: '89', desc: 'running shoes' };
  const PRESETS = [
    preset('Agent buys shoes at an allowed merchant for $99', 'within_mandate'),
    preset("Same, but at a merchant not on the user's list", 'exceeded_mandate', null, SHADY),
    preset('A $150 purchase against a $120 limit', 'exceeded_mandate', null, { amount: '150' }),
    preset('Agent buys a laptop bag when told to buy shoes', 'exceeded_mandate', null, { desc: 'laptop bag', amount: '40' }),
    preset('Trail shoes bought when road shoes were requested', 'ambiguous_intent', { intent: 'buy road running shoes under $120' }, { desc: 'trail running shoes' }),
    preset('The user revoked the mandate before the purchase', 'exceeded_mandate', null, null, { revoke: true }),
    preset('Someone edits a receipt after the fact', 'evidence_invalid', null, SHADY, { edit_receipt: true }),
    preset('A receipt is deleted from the log', 'evidence_invalid', null, SHADY, { delete_earlier: true }),
    preset('The disputed receipt is never supplied', 'evidence_insufficient', null, null, { omit_receipt: true }),
    preset('A mandate signed by someone other than the user', 'evidence_invalid', null, null, { forge_mandate: true }),
  ];

  const Engine = { init: init, runCase: runCase, adjudicate: adjudicate, PRESETS: PRESETS, canon: canon, fmt2: fmt2, fmtG: fmtG };

  /* ---------- the page ---------- */
  function initUI() {
    const $ = (id) => document.getElementById(id);
    if (!$('ev-presets')) return;
    if (!(window.crypto && window.crypto.subtle)) {
      $('ev-out-verdict').innerHTML = '<p class="bad">This demo needs a modern browser with Web Crypto support.</p>';
      return;
    }
    const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
    const VERDICT = {
      within_mandate: ['Within mandate', 'v-within', 'Everything the user signed was respected. A merchant can contest the dispute with this packet.'],
      exceeded_mandate: ['Exceeded mandate', 'v-exceeded', 'The agent broke a rule the user set, so the dispute is valid and liability is routed per the program terms.'],
      ambiguous_intent: ['Ambiguous intent', 'v-ambiguous', 'Every hard rule passed, but it is unclear whether the agent bought what the user wanted. A human decides.'],
      evidence_insufficient: ['Evidence insufficient', 'v-insufficient', 'The record needed to decide was not supplied. Ask for it.'],
      evidence_invalid: ['Evidence invalid', 'v-invalid', 'The record or the mandate cannot be trusted: it was edited, forged or cut. Treat it as possible fraud.'],
    };
    const FIELDS = {
      'ev-f-merchants': ['mandate', 'merchants'], 'ev-f-cap': ['mandate', 'max_per_txn'], 'ev-f-total': ['mandate', 'max_total'], 'ev-f-intent': ['mandate', 'intent'],
      'ev-a-merchant': ['action', 'merchant'], 'ev-a-amount': ['action', 'amount'], 'ev-a-desc': ['action', 'desc'],
    };
    const OPTS = { 'ev-o-revoke': 'revoke', 'ev-o-edit': 'edit_receipt', 'ev-o-delete': 'delete_earlier', 'ev-o-omit': 'omit_receipt', 'ev-o-forge': 'forge_mandate' };
    const fill = (cfg) => {
      for (const id in FIELDS) $(id).value = cfg[FIELDS[id][0]][FIELDS[id][1]];
      for (const id in OPTS) $(id).checked = !!cfg.options[OPTS[id]];
    };
    const read = () => {
      const cfg = { mandate: {}, action: {}, options: {} };
      for (const id in FIELDS) cfg[FIELDS[id][0]][FIELDS[id][1]] = $(id).value;
      for (const id in OPTS) cfg.options[OPTS[id]] = $(id).checked;
      return cfg;
    };
    const render = (d) => {
      const m = d.mandate;
      $('ev-out-mandate').innerHTML = `<dl class="kv"><dt>Allowed merchants</dt><dd>${esc(m.merchants)}</dd><dt>Limit per purchase</dt><dd>$${esc(m.cap)}</dd><dt>Limit in total</dt><dd>$${esc(m.total)}</dd><dt>Valid for</dt><dd>24 hours</dd><dt>What the user asked for</dt><dd>"${esc(m.intent)}"</dd><dt>Signature</dt><dd class="${m.signature_ok ? 'ok' : 'bad'}">${esc(m.signature_note)}</dd></dl>`;
      $('ev-out-receipts').innerHTML = d.receipts.length
        ? `<div class="tw"><table><thead><tr><th>#</th><th>Merchant</th><th>Amount</th><th>Item</th><th>Fingerprint</th><th>Links back to</th><th>Signature</th></tr></thead><tbody>${d.receipts.map((r) => `<tr class="${r.sig_ok ? '' : 'badrow'}"><td>${r.seq}</td><td>${esc(r.merchant)}</td><td>$${esc(r.amount)}</td><td>${esc(r.desc)}</td><td><code>${esc(r.hash)}</code></td><td><code>${esc(r.prev)}</code></td><td class="${r.sig_ok ? 'ok' : 'bad'}">${r.sig_ok ? 'valid' : 'does not verify'}</td></tr>`).join('')}</tbody></table></div>`
        : '<p class="muted">No receipts were supplied.</p>';
      const v = d.verdict, info = VERDICT[v.type];
      $('ev-out-verdict').innerHTML = `<div class="badge ${info[1]}">${info[0]}</div><p>${esc(info[2])}</p><ul>${v.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul><p><strong>Recommended action:</strong> ${esc(v.recommendation)}</p>` +
        (v.intent_score !== null ? `<p class="muted">Intent match score: ${v.intent_score.toFixed(2)} (0.75 or above matches, 0.25 or below does not)</p>` : '');
      $('ev-out-checks').innerHTML = `<div class="tw"><table><thead><tr><th>Result</th><th>Check</th><th>Detail</th></tr></thead><tbody>${d.checks.map((c) => `<tr><td class="${c.passed ? 'ok' : 'bad'}">${c.passed ? 'Pass' : 'Fail'}</td><td>${esc(c.name.replace(/_/g, ' '))}</td><td>${esc(c.detail)}</td></tr>`).join('')}</tbody></table></div>`;
      $('ev-out-packet').textContent = JSON.stringify(d.packet, null, 2);
    };

    let runId = 0;
    const go = async () => {
      const mine = ++runId;
      try {
        const data = await runCase(read());
        if (mine === runId) render(data);
      } catch (err) {
        if (mine === runId) $('ev-out-verdict').innerHTML = '<p class="bad">Something went wrong: ' + esc(err.message) + '</p>';
      }
    };
    const clearActive = () => document.querySelectorAll('#ev-presets button').forEach((b) => { b.classList.remove('on'); b.setAttribute('aria-pressed', 'false'); });

    init(false).then((label) => {
      $('ev-alg').textContent = 'Signatures in this demo use ' + label + '. The Python engine in the repository uses Ed25519.';
      PRESETS.forEach((p) => {
        const b = document.createElement('button');
        b.type = 'button';
        b.textContent = p.name;
        b.setAttribute('aria-pressed', 'false');
        b.addEventListener('click', () => { clearActive(); b.classList.add('on'); b.setAttribute('aria-pressed', 'true'); fill(p.cfg); go(); });
        $('ev-presets').appendChild(b);
      });
      $('ev-run').addEventListener('click', () => { clearActive(); go(); });
      $('ev-presets').firstChild.click();
    });
  }

  if (typeof module !== 'undefined' && module.exports) module.exports = Engine;
  if (typeof document !== 'undefined') {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', initUI);
    else initUI();
  }
})();
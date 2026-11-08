"""One-time patch: adds the "Download the packet as a PDF" button to the website demo.

Run it once from the evenhand folder:   py patch_pdf.py
It changes docs/demo.js, docs/index.html and docs/styles.css. If any check fails it stops and changes nothing.
"""
import sys
from pathlib import Path

PDF_BLOCK = r'''  /* ---------- the evidence packet as a PDF (written by hand, no libraries) ---------- */
  const HELV = [278,278,355,556,556,889,667,191,333,333,389,584,278,333,278,278,556,556,556,556,556,556,556,556,556,556,278,278,584,584,584,556,1015,667,667,722,722,667,611,778,722,278,500,667,556,833,722,778,667,778,722,667,611,722,667,944,667,667,611,278,278,278,469,556,333,556,556,500,556,556,278,556,556,222,222,500,222,833,556,556,556,556,333,500,278,556,500,722,500,500,500,334,260,334,584];
  const HELVB = [278,333,474,556,556,889,722,238,333,333,389,584,278,333,278,278,556,556,556,556,556,556,556,556,556,556,333,333,584,584,584,611,975,722,722,722,722,667,611,778,722,278,556,722,611,833,722,778,667,778,722,667,611,722,667,944,667,667,611,333,278,333,584,556,333,556,611,556,611,556,333,611,611,278,278,556,278,889,611,611,611,611,389,556,333,611,556,778,556,556,500,389,280,389,584];
  const PDFC = {
    ink: [0.075, 0.125, 0.169], teal: [0.043, 0.396, 0.451], muted: [0.31, 0.37, 0.424], rule: [0.722, 0.765, 0.804],
    head: [0.914, 0.929, 0.945], good: [0.106, 0.455, 0.282], bad: [0.69, 0.149, 0.122], warn: [0.522, 0.325, 0],
  };
  const PDF_VERDICTS = {
    within_mandate: ['Within mandate', PDFC.good], exceeded_mandate: ['Exceeded mandate', PDFC.bad],
    ambiguous_intent: ['Ambiguous intent: needs a person', PDFC.warn], evidence_insufficient: ['Evidence insufficient', PDFC.warn],
    evidence_invalid: ['Evidence invalid', PDFC.bad],
  };
  const TRUST_URL = 'https://github.com/franklinvcabreraordonez/Evenhand/blob/main/docs/TRUST-MODEL.md';

  // The standard PDF fonts cover Latin text; anything else becomes "?" so a PDF never fails to build.
  const pdfText = (s) => String(s == null ? '' : s).replace(/[\u2018\u2019]/g, "'").replace(/[\u201c\u201d]/g, '"')
    .replace(/[\u2013\u2014]/g, '-').replace(/[^\x20-\x7e]/g, '?');
  function strWidth(s, font, size) {
    let w = 0;
    for (let i = 0; i < s.length; i++) w += font === 'F3' ? 600 : (font === 'F2' ? HELVB : HELV)[s.charCodeAt(i) - 32];
    return (w * size) / 1000;
  }
  function wrapText(s, font, size, maxW) {
    const lines = [];
    for (const para of pdfText(s).split('\n')) {
      let line = '';
      for (const tok of para.split(' ')) {
        const trial = line ? line + ' ' + tok : tok;
        if (strWidth(trial, font, size) <= maxW) { line = trial; continue; }
        if (line) { lines.push(line); line = ''; }
        let rest = tok;
        while (strWidth(rest, font, size) > maxW) { // a word wider than the line, such as a hash
          let cut = rest.length - 1;
          while (cut > 1 && strWidth(rest.slice(0, cut), font, size) > maxW) cut--;
          lines.push(rest.slice(0, cut));
          rest = rest.slice(cut);
        }
        line = rest;
      }
      lines.push(line);
    }
    return lines;
  }
  const withCommas = (s) => s.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  const pdfMoney = (x) => '$' + withCommas(fmt2(Number(x)));
  const pdfWhen = (ts) => new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';

  function makePdf(packet) {
    const PW = 612, PH = 792, LM = 50, RM = 50, TOP = 42, BOT = 50, CW = PW - LM - RM, C = PDFC;
    const pages = [];
    let ops = [], y = 0, started = false;
    const rgb = (c) => c.map((v) => v.toFixed(3)).join(' ');
    const newPage = () => { if (started) pages.push(ops); ops = []; y = PH - TOP; started = true; };
    const need = (h) => { if (!started || y - h < BOT) newPage(); };
    const text = (x, base, s, font, size, color) => {
      ops.push(`BT /${font} ${size} Tf ${rgb(color)} rg ${x.toFixed(2)} ${base.toFixed(2)} Td (${pdfText(s).replace(/[\\()]/g, '\\$&')}) Tj ET`);
    };
    const rect = (x, bottom, w, h, fill, stroke) => {
      let o = '';
      if (fill) o += `${rgb(fill)} rg `;
      if (stroke) o += `${rgb(stroke)} RG 0.25 w `;
      ops.push(`${o}${x.toFixed(2)} ${bottom.toFixed(2)} ${w.toFixed(2)} ${h.toFixed(2)} re ${fill && stroke ? 'B' : fill ? 'f' : 'S'}`);
    };
    const para = (s, o) => {
      o = Object.assign({ font: 'F1', size: 9.2, lead: 11.6, color: C.ink, before: 0, after: 2 }, o || {});
      const lines = wrapText(s, o.font, o.size, CW);
      need(o.before + o.lead);
      y -= o.before;
      for (const ln of lines) { need(o.lead); text(LM, y - o.size * 0.85, ln, o.font, o.size, o.color); y -= o.lead; }
      y -= o.after;
    };
    const heading = (s) => para(s, { font: 'F2', size: 11, lead: 14, color: C.teal, before: 6, after: 2 });
    function table(rows, widths, o) {
      o = o || {};
      const pad = 2, lead = 10.2;
      const measure = (row, ri) => {
        const cells = row.map((cell, ci) => {
          const c = typeof cell === 'string' ? { t: cell } : cell;
          const font = c.font || ((o.header && ri === 0) || (o.boldFirst && ci === 0) ? 'F2' : 'F1');
          const size = font === 'F3' ? 7.4 : 8.5;
          return { lines: wrapText(c.t, font, size, widths[ci] - 8), font: font, size: size, color: c.color || C.ink };
        });
        return { cells: cells, h: Math.max(...cells.map((c) => c.lines.length)) * lead + pad * 2 };
      };
      const draw = (m, ri) => {
        let x = LM;
        const top = y;
        m.cells.forEach((c, ci) => {
          rect(x, top - m.h, widths[ci], m.h, o.header && ri === 0 ? C.head : null, C.rule);
          c.lines.forEach((ln, li) => text(x + 4, top - pad - c.size * 0.85 - li * lead, ln, c.font, c.size, c.color));
          x += widths[ci];
        });
        y -= m.h;
      };
      const ms = rows.map(measure);
      ms.forEach((m, ri) => {
        const before = pages.length;
        need(m.h);
        if (pages.length !== before && o.header && ri > 0) { need(ms[0].h + m.h); draw(ms[0], 0); }
        draw(m, ri);
      });
    }

    const m = packet.mandate, c = m.constraints || {}, d = packet.disputed_receipt;
    const vd = PDF_VERDICTS[packet.verdict] || [String(packet.verdict).replace(/_/g, ' '), C.ink];
    const merchants = c.merchants_allow != null
      ? (c.merchants_allow.join(', ') || 'none')
      : 'any merchant' + (c.merchants_deny && c.merchants_deny.length ? ' except ' + c.merchants_deny.join(', ') : '');
    const limits = [];
    if (c.max_per_txn != null) limits.push(pdfMoney(c.max_per_txn) + ' per purchase');
    if (c.max_total != null) limits.push(pdfMoney(c.max_total) + ' in total');

    para('Evidence packet', { font: 'F2', size: 18, lead: 22, color: C.teal, after: 0 });
    para('Case ' + packet.case_id + '  |  generated ' + pdfWhen(Date.now() / 1000) + '  |  Evenhand prototype v0.2. Synthetic data unless stated. ' +
      'A recommendation, not a decision: the issuer and the network decide.', { size: 9, lead: 11.5, color: C.muted, after: 8 });
    para(vd[0], { font: 'F2', size: 15, lead: 19, color: vd[1], after: 3 });
    para(packet.recommendation);
    (packet.reasons || []).forEach((r) => para('Reason: ' + r));
    if (packet.verdict === 'evidence_invalid') {
      para('WARNING: the records failed verification. Treat every detail below as unverified: it may have been altered, forged or cut.', { font: 'F2', color: C.bad });
    }
    if (packet.intent_score != null) para('Intent match score: ' + Number(packet.intent_score).toFixed(2) + ' (0.75 or above matches, 0.25 or below does not).', { size: 8, lead: 10, color: C.muted });

    heading('What the cardholder authorized');
    table([
      ['Instruction', '"' + (m.intent || '') + '"'], ['Allowed merchants', merchants],
      ['Allowed categories', c.categories_allow && c.categories_allow.length ? c.categories_allow.join(', ') : 'any'],
      ['Limits', limits.join('; ') || 'none stated'],
      ['Human confirmation needed above', c.step_up_above != null ? pdfMoney(c.step_up_above) : 'not required'], ['Agent', m.agent_id || ''],
    ], [152, 360], { boldFirst: true });

    heading('What the agent did (the disputed action)');
    if (d) {
      const a = d.action;
      const conf = c.step_up_above == null || a.amount <= c.step_up_above ? 'not required' : (a.step_up_done ? 'recorded' : 'NOT recorded');
      table([['#', 'When', 'Merchant', 'Category', 'Items', 'Amount', 'Human confirmation'],
        [String(d.seq), pdfWhen(a.ts), a.merchant_id, a.category, a.cart.map((i) => i.desc).join(', '), pdfMoney(a.amount), conf]],
      [22, 90, 68, 54, 128, 60, 90], { header: true });
    } else {
      para('The disputed action was not among the records supplied, so no purchase detail is shown.');
    }
    para((packet.receipts_supplied || 0) + ' receipt(s) were supplied in the chain.', { size: 8, lead: 10, color: C.muted });

    heading('Every check that ran');
    table([['Result', 'Check', 'Detail']].concat((packet.checks || []).map((ch) => [
      { t: ch.passed ? 'PASS' : 'FAIL', font: 'F2', color: ch.passed ? C.ink : C.bad }, ch.name.replace(/_/g, ' '), ch.detail])),
    [44, 122, 346], { header: true });

    heading('Fingerprints (to verify this packet)');
    const mono = (t) => ({ t: t, font: 'F3' });
    table([['Mandate digest', mono(m.digest || '')], ['User\'s public key', mono(m.principal_pub || '')],
      ['Witness\'s public key', mono(packet.witness_pub || '')], ['Chain head', mono(packet.chain_head || 'none')],
      ['Disputed receipt hash', mono(d ? d.hash : 'not supplied')]], [128, 384], { boldFirst: true });
    heading('How to verify');
    para('1. Get the user\'s public key and the witness\'s public key from sources other than this page. 2. Obtain the exported bundle for this mandate. ' +
      '3. Check it with verify_bundle, pinning both keys, and compare the chain head above. If either key differs, or the check fails, do not rely on this packet.');
    para('Trust model: ' + TRUST_URL, { size: 8, lead: 10, color: C.muted });
    if (started) pages.push(ops);

    // footers, then the file itself
    const footL = 'Evenhand evidence packet ' + packet.case_id + '. Prototype output, not for real disputes.';
    const content = pages.map((p, i) => {
      const pg = 'Page ' + (i + 1);
      const f = [`BT /F1 8 Tf 0.4 g ${LM} 30 Td (${pdfText(footL).replace(/[\\()]/g, '\\$&')}) Tj ET`,
        `BT /F1 8 Tf 0.4 g ${(PW - RM - strWidth(pg, 'F1', 8)).toFixed(2)} 30 Td (${pg}) Tj ET`];
      return p.concat(f).join('\n');
    });
    const objs = [];
    objs[1] = '<< /Type /Catalog /Pages 2 0 R >>';
    const kids = content.map((_, i) => `${6 + 2 * i} 0 R`).join(' ');
    objs[2] = `<< /Type /Pages /Kids [${kids}] /Count ${content.length} >>`;
    objs[3] = '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>';
    objs[4] = '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>';
    objs[5] = '<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>';
    content.forEach((s, i) => {
      objs[6 + 2 * i] = `<< /Type /Page /Parent 2 0 R /MediaBox [0 0 ${PW} ${PH}] /Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> /Contents ${7 + 2 * i} 0 R >>`;
      objs[7 + 2 * i] = `<< /Length ${s.length} >>\nstream\n${s}\nendstream`;
    });
    const infoId = objs.length;
    objs[infoId] = `<< /Title (${pdfText('Evenhand evidence packet ' + packet.case_id).replace(/[\\()]/g, '\\$&')}) /Producer (Evenhand) >>`;
    let out = '%PDF-1.4\n';
    const offsets = [];
    for (let i = 1; i < objs.length; i++) { offsets[i] = out.length; out += `${i} 0 obj\n${objs[i]}\nendobj\n`; }
    const xref = out.length;
    out += `xref\n0 ${objs.length}\n0000000000 65535 f \n`;
    for (let i = 1; i < objs.length; i++) out += String(offsets[i]).padStart(10, '0') + ' 00000 n \n';
    out += `trailer\n<< /Size ${objs.length} /Root 1 0 R /Info ${infoId} 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
    return new TextEncoder().encode(out);
  }

'''

DOWNLOAD_HANDLER = r'''$('ev-run').addEventListener('click', () => { clearActive(); go(); });
      const dl = $('ev-download');
      if (dl) {
        dl.addEventListener('click', () => {
          const note = $('ev-dl-note');
          if (!lastPacket) return;
          try {
            const blob = new Blob([makePdf(lastPacket)], { type: 'application/pdf' });
            const a = document.createElement('a');
            a.href = URL.createObjectURL(blob);
            a.download = 'evenhand-packet-' + lastPacket.case_id + '.pdf';
            document.body.appendChild(a);
            a.click();
            document.body.removeChild(a);
            setTimeout(() => URL.revokeObjectURL(a.href), 2000);
            if (note) note.textContent = '';
          } catch (err) {
            if (note) note.textContent = 'Could not build the PDF: ' + err.message;
          }
        });
      }
'''

BUTTON = r'''<h3>5. The evidence packet a merchant or bank would receive</h3>
              <button id="ev-download" class="btn outline" type="button">Download the packet as a PDF</button>
              <p class="muted" id="ev-dl-note" aria-live="polite"></p>'''

CSS_RULE = "#ev-download { margin: 4px 0 6px; background: transparent; font: inherit; font-weight: 600; cursor: pointer; }\n"


def main():
    demo, html, css = Path("docs/demo.js"), Path("docs/index.html"), Path("docs/styles.css")
    for p in (demo, html, css):
        if not p.exists():
            sys.exit(f"Cannot find {p}. Run this from the evenhand folder (cd $HOME\\projects\\evenhand).")
    s, h, c = (p.read_text(encoding="utf-8") for p in (demo, html, css))
    if "makePdf" in s or "ev-download" in h or "ev-download" in c:
        sys.exit("The files already contain part of this change. Restore them first:\n"
                 "  git checkout <the commit before the PDF edits> -- docs/demo.js docs/index.html docs/styles.css\nthen run this script again.")

    def swap(text, old, new, name):
        n = text.count(old)
        if n != 1:
            sys.exit(f"STOPPED, nothing changed. Expected to find this once in the file but found it {n} times: {name}")
        return text.replace(old, new, 1)

    s = swap(s, "  /* ---------- the page ---------- */", PDF_BLOCK + "  /* ---------- the page ---------- */", "the page section marker in demo.js")
    s = swap(s, "fmtG: fmtG };", "fmtG: fmtG, makePdf: makePdf };", "the Engine line in demo.js")
    s = swap(s, "$('ev-out-packet').textContent = JSON.stringify(d.packet, null, 2);\n",
             "$('ev-out-packet').textContent = JSON.stringify(d.packet, null, 2);\n      lastPacket = d.packet;\n", "the packet line in demo.js")
    s = swap(s, "    let runId = 0;\n", "    let runId = 0;\n    let lastPacket = null;\n", "the runId line in demo.js")
    s = swap(s, "$('ev-run').addEventListener('click', () => { clearActive(); go(); });\n", DOWNLOAD_HANDLER, "the Run button line in demo.js")
    h = swap(h, "<h3>5. The evidence packet a merchant or bank would receive</h3>", BUTTON, "the panel 5 heading in index.html")
    c = c.rstrip("\n") + "\n" + CSS_RULE

    demo.write_text(s, encoding="utf-8")
    html.write_text(h, encoding="utf-8")
    css.write_text(c, encoding="utf-8")
    print("Done. Changed docs/demo.js, docs/index.html and docs/styles.css.")
    print("Next: open docs/index.html with Live Server and click the Download button.")


main()
"""Render an evidence packet as a PDF a merchant, bank or insurer can read.

    from evenhand.report import render_packet_pdf
    render_packet_pdf(gateway.dispute().packet, "packet.pdf")

Needs: pip install reportlab
The PDF is a readable summary of the packet. The packet itself (and the exported bundle) is what a verifier checks.
"""
from __future__ import annotations

import datetime as dt
from typing import Optional

VERDICTS = {
    "within_mandate": ("Within mandate", "#1B7448"),
    "exceeded_mandate": ("Exceeded mandate", "#B0261F"),
    "ambiguous_intent": ("Ambiguous intent: needs a person", "#855300"),
    "evidence_insufficient": ("Evidence insufficient", "#855300"),
    "evidence_invalid": ("Evidence invalid", "#B0261F"),
}
TRUST_URL = "https://github.com/franklinvcabreraordonez/Evenhand/blob/main/docs/TRUST-MODEL.md"


def _safe(text) -> str:
    """The standard PDF fonts cover Latin-1; anything else becomes '?' so rendering never fails."""
    return str(text).encode("latin-1", "replace").decode("latin-1")


def _money(x) -> str:
    return f"${float(x):,.2f}"


def _when(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _limits(c: dict) -> str:
    parts = []
    if c.get("max_per_txn") is not None:
        parts.append(f"{_money(c['max_per_txn'])} per purchase")
    if c.get("max_total") is not None:
        parts.append(f"{_money(c['max_total'])} in total")
    return "; ".join(parts) or "none stated"


def _confirmation(a: dict, c: dict) -> str:
    limit = c.get("step_up_above")
    if limit is None or a["amount"] <= limit:
        return "not required"
    return "recorded" if a["step_up_done"] else "NOT recorded"


def _merchants(c: dict) -> str:
    if c.get("merchants_allow") is not None:
        return ", ".join(c["merchants_allow"]) or "none"
    deny = c.get("merchants_deny") or []
    return "any merchant" + (f" except {', '.join(deny)}" if deny else "")


def render_packet_pdf(packet: dict, dest, *, generated_at: Optional[float] = None) -> None:
    """Write the packet as a PDF to a file path or a binary file object."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import letter
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("PDF output needs reportlab: pip install reportlab") from exc
    from xml.sax.saxutils import escape

    verdict = packet["verdict"]
    label, hexcolor = VERDICTS.get(verdict, (verdict.replace("_", " ").title(), "#13202B"))
    teal, ink, muted, rule = colors.HexColor("#0B6573"), colors.HexColor("#13202B"), colors.HexColor("#4F5F6C"), colors.HexColor("#B8C3CD")
    S = {
        "title": ParagraphStyle("t", fontName="Helvetica-Bold", fontSize=18, leading=22, textColor=teal),
        "case": ParagraphStyle("c", fontName="Helvetica", fontSize=9.5, leading=12, textColor=muted, spaceAfter=8),
        "badge": ParagraphStyle("b", fontName="Helvetica-Bold", fontSize=15, leading=19, textColor=colors.HexColor(hexcolor)),
        "h": ParagraphStyle("h", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=teal, spaceBefore=7, spaceAfter=2),
        "body": ParagraphStyle("p", fontName="Helvetica", fontSize=9.2, leading=11.6, textColor=ink, spaceAfter=2),
        "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.5, leading=10.5, textColor=ink),
        "cellb": ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=8.5, leading=10.5, textColor=ink),
        "mono": ParagraphStyle("m", fontName="Courier", fontSize=7.4, leading=9, textColor=ink),
        "warn": ParagraphStyle("w", fontName="Helvetica-Bold", fontSize=9.2, leading=11.6, textColor=colors.HexColor("#B0261F"), spaceAfter=2),
        "small": ParagraphStyle("s", fontName="Helvetica", fontSize=8, leading=10, textColor=muted),
    }
    P = lambda text, style: Paragraph(escape(_safe(text)), S[style])

    def grid(rows, widths, header=True):
        t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
        style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                 ("GRID", (0, 0), (-1, -1), 0.25, rule)]
        if header:
            style.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E9EDF1")))
        t.setStyle(TableStyle(style))
        return t

    m, c = packet["mandate"], packet["mandate"].get("constraints", {})
    story = [P("Evidence packet", "title"),
             P(f"Case {packet['case_id']}  |  generated {_when(generated_at or dt.datetime.now(dt.timezone.utc).timestamp())}  |  "
               "Evenhand prototype v0.2. Synthetic data unless stated. A recommendation, not a decision: the issuer and the network decide.", "case"),
             Paragraph(escape(label), S["badge"]), Spacer(1, 3), P(packet["recommendation"], "body")]
    for r in packet.get("reasons", []):
        story.append(P("Reason: " + r, "body"))
    if verdict == "evidence_invalid":
        story.append(P("WARNING: the records failed verification. Treat every detail below as unverified: it may have been altered, forged or cut.", "warn"))
    if packet.get("intent_score") is not None:
        story.append(P(f"Intent match score: {packet['intent_score']:.2f} (0.75 or above matches, 0.25 or below does not).", "small"))

    story.append(P("What the cardholder authorized", "h"))
    auth = [("Instruction", '"' + str(m.get("intent", "")) + '"'), ("Allowed merchants", _merchants(c)),
            ("Allowed categories", ", ".join(c["categories_allow"]) if c.get("categories_allow") else "any"), ("Limits", _limits(c)),
            ("Human confirmation needed above", _money(c["step_up_above"]) if c.get("step_up_above") is not None else "not required"),
            ("Agent", m.get("agent_id", ""))]
    story.append(grid([[P(k, "cellb"), P(v, "cell")] for k, v in auth], [1.9 * inch, 5.2 * inch], header=False))

    story.append(P("What the agent did (the disputed action)", "h"))
    d = packet.get("disputed_receipt")
    if d:
        a = d["action"]
        rows = [[P(h, "cellb") for h in ("#", "When", "Merchant", "Category", "Items", "Amount", "Human confirmation")],
                [P(d["seq"], "cell"), P(_when(a["ts"]), "cell"), P(a["merchant_id"], "cell"), P(a["category"], "cell"),
                 P(", ".join(i["desc"] for i in a["cart"]), "cell"), P(_money(a["amount"]), "cell"), P(_confirmation(a, c), "cell")]]
        story.append(grid(rows, [0.3 * inch, 1.25 * inch, 0.95 * inch, 0.75 * inch, 1.75 * inch, 0.85 * inch, 1.25 * inch]))
    else:
        story.append(P("The disputed action was not among the records supplied, so no purchase detail is shown.", "body"))
    story.append(P(f"{packet.get('receipts_supplied', 0)} receipt(s) were supplied in the chain.", "small"))

    story.append(P("Every check that ran", "h"))
    rows = [[P(h, "cellb") for h in ("Result", "Check", "Detail")]]
    for ch in packet.get("checks", []):
        rows.append([P("PASS" if ch["passed"] else "FAIL", "cellb"), P(ch["name"].replace("_", " "), "cell"), P(ch["detail"], "cell")])
    story.append(grid(rows, [0.6 * inch, 1.7 * inch, 4.8 * inch]))

    story.append(KeepTogether([
        P("Fingerprints (to verify this packet)", "h"),
        grid([[P("Mandate digest", "cellb"), P(m.get("digest", ""), "mono")], [P("User's public key", "cellb"), P(m.get("principal_pub", ""), "mono")],
              [P("Witness's public key", "cellb"), P(packet.get("witness_pub", ""), "mono")], [P("Chain head", "cellb"), P(packet.get("chain_head") or "none", "mono")],
              [P("Disputed receipt hash", "cellb"), P(d["hash"] if d else "not supplied", "mono")]], [1.7 * inch, 5.4 * inch], header=False),
        P("How to verify", "h"),
        P("1. Get the user's public key and the witness's public key from sources other than this page. 2. Obtain the exported bundle for this mandate. "
          "3. Check it with verify_bundle, pinning both keys, and compare the chain head above. If either key differs, or the check fails, do not rely on this packet.", "body"),
        P("Trust model: " + TRUST_URL, "small")]))

    def footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillGray(0.4)
        canvas.drawString(0.7 * inch, 0.45 * inch, f"Evenhand evidence packet {packet['case_id']}. Prototype output, not for real disputes.")
        canvas.drawRightString(letter[0] - 0.7 * inch, 0.45 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(dest, pagesize=letter, leftMargin=0.7 * inch, rightMargin=0.7 * inch, topMargin=0.5 * inch, bottomMargin=0.65 * inch,
                            title=f"Evenhand evidence packet {packet['case_id']}", author="Evenhand")
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
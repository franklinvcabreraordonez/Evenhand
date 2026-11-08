"""Write evidence-packet PDFs for a few disputes.

Run from the repository root:  python examples/make_packet_pdf.py
Needs: pip install reportlab
Creates the folder out/ (ignored by git) with one PDF per verdict.
"""
import dataclasses
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # works without installing

from evenhand import Gateway, quick_mandate
from evenhand.mandate import sign_revocation
from evenhand.report import render_packet_pdf

os.makedirs("out", exist_ok=True)
signed, principal = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"], max_per_txn=120, categories=["sports"])
gw = Gateway(signed, expected_principal_pub=principal.public_hex)
gw.record(merchant="stridemart", amount=99.0, description="road running shoes", category="sports")
gw.record(merchant="shadymart", amount=89.0, description="running shoes", category="sports")

for name, dispute in (("within-mandate", gw.dispute(0)), ("exceeded-mandate", gw.dispute(1))):
    path = f"out/packet-{name}.pdf"
    render_packet_pdf(dispute.packet, path)
    print("wrote", path, "-", dispute.verdict.type.value)

# a tampered record: someone edits receipt 1 after the fact
forged = list(gw.receipts)
forged[1] = dataclasses.replace(forged[1], action=dataclasses.replace(forged[1].action, merchant_id="stridemart"))
from evenhand.adjudicator import adjudicate
from evenhand.evidence import build_packet
verdict = adjudicate(signed, forged, 1, gw.witness_pub, expected_principal_pub=principal.public_hex)
render_packet_pdf(build_packet("case-tampered", signed, forged, 1, verdict, gw.witness_pub), "out/packet-evidence-invalid.pdf")
print("wrote out/packet-evidence-invalid.pdf -", verdict.type.value)
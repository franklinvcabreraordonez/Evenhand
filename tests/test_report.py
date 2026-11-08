import dataclasses
import io
import unittest

from evenhand import Gateway, quick_mandate
from evenhand.adjudicator import adjudicate
from evenhand.evidence import build_packet

try:
    import pypdf
    import reportlab  # noqa: F401
    from evenhand.report import render_packet_pdf
    HAVE_LIBS = True
except ImportError:
    HAVE_LIBS = False


def text_of(packet):
    buf = io.BytesIO()
    render_packet_pdf(packet, buf)
    data = buf.getvalue()
    reader = pypdf.PdfReader(io.BytesIO(data))
    return data, len(reader.pages), " ".join(p.extract_text() for p in reader.pages)


@unittest.skipUnless(HAVE_LIBS, "needs reportlab and pypdf")
class PacketPdf(unittest.TestCase):
    def setUp(self):
        self.signed, self.principal = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"], max_per_txn=120, categories=["sports"])
        self.gw = Gateway(self.signed, expected_principal_pub=self.principal.public_hex)
        self.gw.record(merchant="stridemart", amount=99.0, description="road running shoes", category="sports")
        self.gw.record(merchant="shadymart", amount=89.0, description="running shoes", category="sports")

    def test_a_clean_one_page_pdf_with_the_facts(self):
        d = self.gw.dispute(1)
        data, pages, text = text_of(d.packet)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertEqual(pages, 1)
        flat = " ".join(text.split())
        for needle in ("Evidence packet", d.packet["case_id"], "Exceeded mandate", "shadymart", "$89.00", "FAIL", "merchant allowed",
                       d.packet["mandate"]["digest"][:20], "recommendation, not a decision"):
            self.assertIn(needle, flat, needle)
        self.assertIn(d.packet["chain_head"][:20], flat)

    def test_every_verdict_renders(self):
        seen = set()
        for seq in (0, 1):
            p = self.gw.dispute(seq).packet
            seen.add(p["verdict"])
            self.assertGreaterEqual(text_of(p)[1], 1)
        forged = list(self.gw.receipts)
        forged[1] = dataclasses.replace(forged[1], action=dataclasses.replace(forged[1].action, merchant_id="stridemart"))
        v = adjudicate(self.signed, forged, 1, self.gw.witness_pub, expected_principal_pub=self.principal.public_hex)
        p = build_packet("case-x", self.signed, forged, 1, v, self.gw.witness_pub)
        _, _, text = text_of(p)
        self.assertIn("WARNING", text)  # failed verification is flagged on the page
        self.assertIn("Evidence invalid", text)
        missing = build_packet("case-m", self.signed, self.gw.receipts[:1], 1, adjudicate(self.signed, self.gw.receipts[:1], 1, self.gw.witness_pub), self.gw.witness_pub)
        _, _, text = text_of(missing)
        self.assertIn("not among the records supplied", " ".join(text.split()))
        seen.update({p["verdict"], missing["verdict"]})
        self.assertEqual(seen, {"within_mandate", "exceeded_mandate", "evidence_invalid", "evidence_insufficient"})

    def test_unusual_text_does_not_break_rendering(self):
        self.gw.record(merchant="Caf\u00e9 \u5e97 <b>x</b> & co", amount=5, description="\u201csmart\u201d quotes & <tags> \u2014 and a very long description " * 12, category="sports")
        data, pages, text = text_of(self.gw.dispute().packet)
        self.assertTrue(data.startswith(b"%PDF"))
        self.assertIn("<b>x</b>", " ".join(text.split()))  # shown literally, never interpreted as markup

    def test_writes_to_a_file_path(self):
        import os
        import tempfile
        path = os.path.join(tempfile.mkdtemp(), "packet.pdf")
        render_packet_pdf(self.gw.dispute(0).packet, path)
        self.assertGreater(os.path.getsize(path), 1000)


if __name__ == "__main__":
    unittest.main()
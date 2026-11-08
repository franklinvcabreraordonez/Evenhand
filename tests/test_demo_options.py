"""Every "what goes wrong" option in the demo must change the outcome, for any merchant and amount."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
import web_demo  # noqa: E402

BASE = {"mandate": {"merchants": "stridemart", "max_per_txn": "120", "max_total": "300", "intent": "buy running shoes under $120"},
        "action": {"merchant": "stridemart", "amount": "40", "desc": "road running shoes"}}
EXPECTED = {"revoke": "exceeded_mandate", "edit_receipt": "evidence_invalid", "delete_earlier": "evidence_invalid",
            "omit_receipt": "evidence_insufficient", "forge_mandate": "evidence_invalid"}


def verdict(merchant, amount, option=None):
    cfg = {"mandate": dict(BASE["mandate"]), "action": dict(BASE["action"], merchant=merchant, amount=str(amount)), "options": {option: True} if option else {}}
    return web_demo.run_case(cfg)["verdict"]["type"]


class EveryOptionChangesTheOutcome(unittest.TestCase):
    def test_each_option_gives_its_verdict_whatever_the_merchant_or_amount(self):
        for merchant in ("stridemart", "shadymart"):
            for amount in (0, 1, 40, 120, 150, 5000):
                for option, expected in EXPECTED.items():
                    self.assertEqual(verdict(merchant, amount, option), expected, f"{option} with {merchant} at {amount}")

    def test_editing_a_receipt_that_was_already_fine_is_still_caught(self):
        self.assertEqual(verdict("stridemart", 40), "within_mandate")
        self.assertEqual(verdict("stridemart", 40, "edit_receipt"), "evidence_invalid")


if __name__ == "__main__":
    unittest.main()

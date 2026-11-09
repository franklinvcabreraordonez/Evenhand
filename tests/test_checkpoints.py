import dataclasses
import json
import unittest

from evenhand import Gateway, quick_mandate, verify_bundle
from evenhand.checkpoint import (Checkpoint, check_against_checkpoint, checkpoint_from_dict, checkpoint_to_dict,
                                 make_checkpoint, verify_bundle_with_checkpoint, verify_checkpoint)
from evenhand.crypto import Signer
from evenhand.receipts import AgentAction, CartItem, Witness


def gateway_with(n):
    signed, principal = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"],
                                      max_per_txn=120, categories=["sports"], mandate_id="m-cp")
    gw = Gateway(signed, expected_principal_pub=principal.public_hex)
    for i in range(n):
        gw.record(merchant="stridemart", amount=10 + i, description="road running shoes", category="sports")
    return gw, principal


class CheckpointBasics(unittest.TestCase):
    def setUp(self):
        self.gw, self.principal = gateway_with(3)
        self.cp = make_checkpoint(self.gw.witness.signer, "m-cp", self.gw.receipts, 1_800_000_000)

    def test_a_checkpoint_covers_the_chain_and_verifies(self):
        self.assertEqual((self.cp.count, self.cp.head, self.cp.witness_pub), (3, self.gw.receipts[-1].hash, self.gw.witness_pub))
        self.assertTrue(verify_checkpoint(self.cp))
        self.assertTrue(verify_checkpoint(self.cp, self.gw.witness_pub))
        self.assertFalse(verify_checkpoint(self.cp, "00" * 32))          # not the witness you chose

    def test_it_needs_a_receipt_and_a_contiguous_chain(self):
        with self.assertRaises(ValueError):
            make_checkpoint(self.gw.witness.signer, "m-cp", [], 1)
        with self.assertRaises(ValueError):
            make_checkpoint(self.gw.witness.signer, "m-cp", self.gw.receipts[1:], 1)

    def test_every_field_is_covered_by_the_signature(self):
        changes = {"mandate_id": "m-other", "count": 2, "head": "ab" * 32, "issued_at": 5, "witness_pub": Signer().public_hex}
        for field, value in changes.items():
            self.assertFalse(verify_checkpoint(dataclasses.replace(self.cp, **{field: value})), field)
        self.assertFalse(verify_checkpoint(dataclasses.replace(self.cp, signature="00" * 64)))

    def test_json_round_trip_and_strict_validation(self):
        again = checkpoint_from_dict(json.loads(json.dumps(checkpoint_to_dict(self.cp))))
        self.assertEqual(again, self.cp)
        good = checkpoint_to_dict(self.cp)
        for key, bad in (("count", 0), ("count", True), ("count", 2.5), ("head", "xyz"), ("issued_at", -1), ("signature", "ab"),
                         ("witness_pub", 7), ("mandate_id", ""), ("format", "other")):
            with self.assertRaisesRegex(ValueError, key):
                checkpoint_from_dict(dict(good, **{key: bad}))
        missing = dict(good)
        del missing["head"]
        with self.assertRaisesRegex(ValueError, "head"):
            checkpoint_from_dict(missing)
        with self.assertRaises(ValueError):
            checkpoint_from_dict("not a checkpoint")


class AgainstChains(unittest.TestCase):
    def test_a_longer_chain_still_contains_the_checkpoint(self):
        gw, _ = gateway_with(3)
        cp = make_checkpoint(gw.witness.signer, "m-cp", gw.receipts, 1)
        gw.record(merchant="stridemart", amount=5, description="road running shoes", category="sports")
        self.assertEqual(check_against_checkpoint(gw.receipts, cp), [])
        self.assertEqual(check_against_checkpoint(gw.receipts[:3], cp), [])

    def test_receipts_cut_off_the_end_are_caught(self):
        gw, _ = gateway_with(4)
        cp = make_checkpoint(gw.witness.signer, "m-cp", gw.receipts, 1)
        problems = check_against_checkpoint(gw.receipts[:2], cp)
        self.assertEqual(len(problems), 1)
        self.assertIn("cut off", problems[0])

    def test_rewritten_history_is_caught(self):
        gw, _ = gateway_with(3)
        cp = make_checkpoint(gw.witness.signer, "m-cp", gw.receipts, 1)
        rival = Witness(gw.witness.signer)       # the same witness key tells a different story
        for amount in (10, 99, 12):
            rival.record(AgentAction("m-cp", "agent-1", "purchase", float(amount), "USD", "stridemart", "sports",
                                     (CartItem("sku-1", "road running shoes", float(amount), 1),), False, 1.0))
        problems = check_against_checkpoint(rival.chain("m-cp"), cp)
        self.assertEqual(len(problems), 1)
        self.assertIn("rewritten", problems[0])


class Bundles(unittest.TestCase):
    def setUp(self):
        self.gw, self.principal = gateway_with(3)
        self.cp = make_checkpoint(self.gw.witness.signer, "m-cp", self.gw.receipts, 1)
        self.keys = dict(expected_witness_pub=self.gw.witness_pub, expected_principal_pub=self.principal.public_hex)

    def bundle(self):
        return json.loads(json.dumps(self.gw.export()))

    def test_a_chain_that_has_grown_passes_and_reports_how_much(self):
        self.gw.record(merchant="stridemart", amount=7, description="road running shoes", category="sports")
        out = verify_bundle_with_checkpoint(self.bundle(), checkpoint_to_dict(self.cp), **self.keys)
        self.assertEqual((out["ok"], out["checkpoint_count"], out["receipts_after_checkpoint"]), (True, 3, 1))

    def test_a_truncated_chain_verifies_alone_but_not_against_the_checkpoint(self):
        short = dict(self.bundle(), receipts=self.bundle()["receipts"][:-2], head=None)
        self.assertTrue(verify_bundle(short, **self.keys)["ok"])                    # plain verification is fooled
        out = verify_bundle_with_checkpoint(short, self.cp, **self.keys)            # a saved checkpoint is not
        self.assertFalse(out["ok"])
        self.assertTrue(any("cut off" in p for p in out["problems"]))

    def test_a_checkpoint_from_another_witness_or_a_tampered_one_fails(self):
        stranger = make_checkpoint(Signer(), "m-cp", self.gw.receipts, 1)
        out = verify_bundle_with_checkpoint(self.bundle(), stranger, **self.keys)
        self.assertFalse(out["ok"])
        self.assertTrue(any("different witness key" in p for p in out["problems"]))
        forged = dataclasses.replace(self.cp, count=1)                                # looks weaker, signature no longer matches
        out = verify_bundle_with_checkpoint(self.bundle(), forged, **self.keys)
        self.assertFalse(out["ok"])
        self.assertTrue(any("signature does not verify" in p for p in out["problems"]))

    def test_a_checkpoint_for_another_mandate_fails(self):
        other, _ = gateway_with(3)
        other_cp = make_checkpoint(other.witness.signer, "m-other", other.receipts, 1)
        out = verify_bundle_with_checkpoint(self.bundle(), other_cp, expected_witness_pub=other.witness_pub)
        self.assertFalse(out["ok"])

    def test_malformed_inputs_fail_cleanly(self):
        out = verify_bundle_with_checkpoint(self.bundle(), {"format": "evenhand-checkpoint-v1"}, **self.keys)
        self.assertFalse(out["ok"])
        self.assertTrue(any("malformed checkpoint" in p for p in out["problems"]))
        out = verify_bundle_with_checkpoint({"format": "evenhand-bundle-v1"}, self.cp, **self.keys)
        self.assertFalse(out["ok"])
        out = verify_bundle_with_checkpoint("not a bundle", self.cp, **self.keys)
        self.assertFalse(out["ok"])

    def test_the_witness_key_must_be_given(self):
        with self.assertRaises(TypeError):
            verify_bundle_with_checkpoint(self.bundle(), self.cp)


if __name__ == "__main__":
    unittest.main()
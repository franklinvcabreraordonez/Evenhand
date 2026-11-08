import json
import unittest

from evenhand import Gateway, quick_mandate, verify_bundle
from evenhand.intent import RobustIntentMatcher, near_word, normalize, same_word

M = RobustIntentMatcher()


def score(intent, desc):
    return M.score(intent, [{"desc": desc, "price": 50.0}])


class RobustMatcher(unittest.TestCase):
    def test_exact_and_brand_names_match(self):
        self.assertEqual(score("buy running shoes under $120", "running shoes"), 1.0)
        self.assertEqual(score("buy running shoes under $120", "Nike road running shoes"), 1.0)

    def test_typos_match_when_the_word_is_long_enough(self):
        self.assertEqual(score("buy running shoes under $120", "runnng shoes"), 1.0)          # a letter missing
        self.assertEqual(score("buy wireless headphones under $80", "wireless headphnes"), 1.0)
        self.assertEqual(score("buy running shoes under $120", "rnuning shoes"), 1.0)          # two letters swapped

    def test_a_bundle_matches_when_one_item_is_what_was_asked(self):
        for sep in ("+", "&", ",", "and", "with", "plus"):
            self.assertEqual(score("buy running shoes under $120", f"running shoes {sep} running socks"), 1.0, sep)

    def test_common_synonyms_match(self):
        self.assertEqual(score("buy a laptop under $900", "notebook computer"), 1.0)
        self.assertEqual(score("buy a phone under $500", "smartphone"), 1.0)
        self.assertEqual(score("buy coffee beans under $20", "ground coffee"), 1.0)
        self.assertEqual(score("buy yoga mat under $40", "exercise mat"), 1.0)

    def test_look_alike_products_still_fail(self):
        for intent, desc in (("buy running shoes under $120", "running socks"), ("buy a laptop under $900", "laptop bag"),
                             ("buy wireless headphones under $80", "headphone case"), ("buy yoga mat under $40", "yoga strap")):
            self.assertLessEqual(score(intent, desc), 0.25, desc)
        self.assertLessEqual(score("buy running shoes under $120", "runnng sokcs"), 0.25)    # a typo does not rescue it

    def test_a_different_qualifier_is_unsure_and_an_unrelated_product_fails(self):
        s = score("buy road running shoes under $120", "trail running shoes")
        self.assertTrue(0.25 < s < 0.75, s)
        self.assertLessEqual(score("buy yoga mat under $40", "laptop"), 0.25)

    def test_a_possible_typo_in_a_short_word_goes_to_a_person_not_a_rejection(self):
        self.assertEqual(score("buy a phone under $500", "phne"), 0.5)
        self.assertEqual(score("buy yoga mat under $40", "ygoa mat"), 0.5)

    def test_unparseable_intents_are_unsure(self):
        self.assertEqual(score("a nice gift for my mom, around $50", "scented candle"), 0.5)
        self.assertEqual(M.score("buy running shoes under $120", []), 0.5)

    def test_a_known_limit_unknown_synonyms_are_rejected(self):
        # "desk seat" for "office chair" is not in the table. This documents the limit: a model or a person is the fix.
        self.assertLessEqual(score("buy an office chair under $200", "desk seat"), 0.25)

    def test_word_helpers(self):
        self.assertTrue(same_word("running", "runnng") and not same_word("mat", "mt"))
        self.assertTrue(near_word("phone", "phne") and not near_word("mat", "mt"))
        self.assertEqual(normalize("Notebook computers"), ["laptop"])

    def test_the_adjudicator_uses_it_by_default(self):
        signed, principal = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"], max_per_txn=120)
        gw = Gateway(signed, expected_principal_pub=principal.public_hex)
        gw.record(merchant="stridemart", amount=60, description="Nike runnng shoes + running socks")
        self.assertEqual(gw.dispute().verdict.type.value, "within_mandate")


class WitnessKeyPinning(unittest.TestCase):
    def setUp(self):
        self.signed, self.principal = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"], max_per_txn=120)
        self.real = Gateway(self.signed, expected_principal_pub=self.principal.public_hex)
        self.real.record(merchant="stridemart", amount=50, description="running shoes")

    def test_a_forged_chain_under_another_witness_passes_unless_you_pin_the_witness(self):
        fake = Gateway(self.signed)  # attacker: the user's genuine public mandate, receipts under their own witness
        fake.record(merchant="shadymart", amount=500, description="gift cards")
        bundle = json.loads(json.dumps(fake.export()))
        unpinned = verify_bundle(bundle, expected_principal_pub=self.principal.public_hex)
        self.assertTrue(unpinned["ok"])  # internally consistent, which is all this proves
        pinned = verify_bundle(bundle, expected_principal_pub=self.principal.public_hex, expected_witness_pub=self.real.witness_pub)
        self.assertFalse(pinned["ok"])
        self.assertIn("unexpected witness key", " ".join(pinned["problems"]))

    def test_the_real_chain_passes_when_pinned_and_reports_its_keys(self):
        out = verify_bundle(json.loads(json.dumps(self.real.export())), expected_principal_pub=self.principal.public_hex,
                            expected_witness_pub=self.real.witness_pub)
        self.assertTrue(out["ok"])
        self.assertEqual((out["principal_pub"], out["witness_pub"]), (self.principal.public_hex, self.real.witness_pub))


if __name__ == "__main__":
    unittest.main()
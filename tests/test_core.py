import dataclasses
import unittest

from evenhand.adjudicator import VerdictType, adjudicate
from evenhand.crypto import Signer
from evenhand.mandate import (Constraints, Mandate, sign_mandate, sign_revocation, verify_mandate)
from evenhand.receipts import AgentAction, CartItem, Witness, verify_chain
from evenhand.simulator import generate

T0 = 1_800_000_000.0
DAY = 86_400.0


def make(intent="buy running shoes under $120", **cons):
    principal, witness = Signer(), Witness()
    base = dict(max_per_txn=120.0, max_total=300.0, merchants_allow=("stridemart",), categories_allow=("sports",))
    base.update(cons)
    m = Mandate("m1", "p1", "agent-1", T0, T0 + 30 * DAY, ("purchase",), Constraints(**base), intent)
    return principal, witness, m, sign_mandate(m, principal)


def act(m, amount=100.0, merchant="stridemart", desc="running shoes", ts=T0 + DAY, step=True, cat="sports"):
    return AgentAction(m.mandate_id, m.agent_id, "purchase", amount, "USD", merchant, cat,
                       (CartItem("s", desc, amount),), step, ts)


def judge(principal, witness, m, sm, **kw):
    chain = witness.chain(m.mandate_id)
    return adjudicate(sm, chain, len(chain) - 1, witness.public_hex,
                      expected_principal_pub=principal.public_hex, **kw)


class Crypto(unittest.TestCase):
    def test_mandate_signature_detects_edit(self):
        _, _, m, sm = make()
        self.assertTrue(verify_mandate(sm))
        bad = dataclasses.replace(sm, mandate=dataclasses.replace(m, intent="buy anything"))
        self.assertFalse(verify_mandate(bad))


class Chain(unittest.TestCase):
    def setUp(self):
        self.p, self.w, self.m, self.sm = make()
        for k in range(3):
            self.w.record(act(self.m, 10 + k, ts=T0 + DAY + k))
        self.chain = self.w.chain("m1")

    def test_intact(self):
        self.assertTrue(verify_chain(self.chain, self.w.public_hex, "m1", self.w.head("m1")).ok)

    def test_edit_delete_reorder_truncate(self):
        pub = self.w.public_hex
        edited = list(self.chain)
        edited[1] = dataclasses.replace(edited[1], action=dataclasses.replace(edited[1].action, amount=1.0))
        self.assertFalse(verify_chain(edited, pub, "m1").ok)
        self.assertFalse(verify_chain(self.chain[1:], pub, "m1").ok)
        self.assertFalse(verify_chain([self.chain[1], self.chain[0], self.chain[2]], pub, "m1").ok)
        self.assertFalse(verify_chain(self.chain[:-1], pub, "m1", self.w.head("m1")).ok)  # head checkpoint catches it

    def test_wrong_witness_key(self):
        self.assertFalse(verify_chain(self.chain, Signer().public_hex, "m1").ok)


class Rules(unittest.TestCase):
    def verdict(self, **kw):
        p, w, m, sm = make(**kw.pop("cons", {}))
        revs = kw.pop("revs", False)
        action = act(m, **kw)
        w.record(action)
        r = [sign_revocation("m1", action.ts - 3600, p)] if revs else []
        return judge(p, w, m, sm, revocations=r).type

    def test_within(self):
        self.assertEqual(self.verdict(), VerdictType.WITHIN_MANDATE)

    def test_over_cap(self):
        self.assertEqual(self.verdict(amount=150.0), VerdictType.EXCEEDED_MANDATE)

    def test_merchant(self):
        self.assertEqual(self.verdict(merchant="shadymart"), VerdictType.EXCEEDED_MANDATE)

    def test_expired(self):
        self.assertEqual(self.verdict(ts=T0 + 40 * DAY), VerdictType.EXCEEDED_MANDATE)

    def test_revoked(self):
        self.assertEqual(self.verdict(revs=True), VerdictType.EXCEEDED_MANDATE)

    def test_step_up(self):
        self.assertEqual(self.verdict(cons={"step_up_above": 50.0}, amount=100.0, step=False),
                         VerdictType.EXCEEDED_MANDATE)
        self.assertEqual(self.verdict(cons={"step_up_above": 50.0}, amount=100.0, step=True),
                         VerdictType.WITHIN_MANDATE)

    def test_cumulative(self):
        p, w, m, sm = make(max_total=150.0)
        w.record(act(m, 100.0))
        w.record(act(m, 100.0, ts=T0 + DAY + 1))
        self.assertEqual(judge(p, w, m, sm).type, VerdictType.EXCEEDED_MANDATE)

    def test_ambiguous_and_mismatch(self):
        self.assertEqual(self.verdict(desc="trail running shoes"), VerdictType.WITHIN_MANDATE)  # no qualifier in intent
        p, w, m, sm = make(intent="buy road running shoes under $120")
        w.record(act(m, desc="trail running shoes"))
        self.assertEqual(judge(p, w, m, sm).type, VerdictType.AMBIGUOUS_INTENT)
        p, w, m, sm = make()
        w.record(act(m, desc="running socks", amount=20.0))
        self.assertEqual(judge(p, w, m, sm).type, VerdictType.EXCEEDED_MANDATE)

    def test_forged_mandate_and_missing_receipt(self):
        p, w, m, sm = make()
        w.record(act(m))
        forged = sign_mandate(m, Signer())
        chain = w.chain("m1")
        self.assertEqual(adjudicate(forged, chain, 0, w.public_hex, expected_principal_pub=p.public_hex).type,
                         VerdictType.EVIDENCE_INVALID)
        self.assertEqual(adjudicate(sm, [], 0, w.public_hex, expected_principal_pub=p.public_hex).type,
                         VerdictType.EVIDENCE_INSUFFICIENT)


class Sim(unittest.TestCase):
    def test_deterministic(self):
        a = [(c.scenario, c.label) for c in generate(60, 3)]
        b = [(c.scenario, c.label) for c in generate(60, 3)]
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()

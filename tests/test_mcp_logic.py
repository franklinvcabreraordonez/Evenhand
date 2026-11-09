import os
import random
import tempfile
import threading
import unittest
from decimal import Decimal

from evenhand import Gateway, quick_mandate
from evenhand.mandate import Constraints, Mandate
from evenhand.crypto import Signer
from evenhand.mcp import rpc
from evenhand.mcp.audit import AuditLog
from evenhand.mcp.config import ConfigError, ToolSpec, config_from_dict, dig, extract, to_amount, to_text
from evenhand.mcp.policy import Policy

NOW = 1_800_000_000.0


class RpcTests(unittest.TestCase):
    def test_classifies_messages(self):
        self.assertTrue(rpc.is_request({"jsonrpc": "2.0", "id": 1, "method": "tools/call"}))
        self.assertFalse(rpc.is_request({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertTrue(rpc.is_response({"jsonrpc": "2.0", "id": 1, "result": {}}))
        self.assertTrue(rpc.is_response({"jsonrpc": "2.0", "id": "a", "error": {"code": -1, "message": "x"}}))
        self.assertFalse(rpc.is_response({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
        self.assertNotEqual(rpc.key(1), rpc.key("1"))

    def test_parse_and_dumps(self):
        self.assertIsNone(rpc.parse(b"not json"))
        self.assertIsNone(rpc.parse(b"\xff\xfe"))
        self.assertEqual(rpc.parse(b'{"a": 1}'), {"a": 1})
        self.assertEqual(rpc.dumps({"a": "caf\u00e9", "b": [1, 2]}), '{"a":"caf\u00e9","b":[1,2]}'.encode("utf-8"))
        self.assertNotIn(b"\n", rpc.dumps({"text": "line\nbreak"}))

    def test_tool_call_name(self):
        self.assertEqual(rpc.tool_call_name({"method": "tools/call", "params": {"name": "buy"}}), "buy")
        self.assertIsNone(rpc.tool_call_name({"method": "tools/list"}))
        self.assertIsNone(rpc.tool_call_name({"method": "tools/call", "params": {"name": 5}}))
        self.assertIsNone(rpc.tool_call_name([1]))

    def test_the_outcome_of_a_response(self):
        out = lambda r: rpc.outcome(r)[0]
        self.assertEqual(out({"id": 1, "result": {"content": []}}), "complete")                       # a legacy server
        self.assertEqual(out({"id": 1, "result": {"resultType": "complete", "content": []}}), "complete")
        self.assertEqual(out({"id": 1, "result": {"resultType": "complete", "isError": True}}), "tool_error")
        self.assertEqual(out({"id": 1, "result": {"isError": True, "content": []}}), "tool_error")
        self.assertEqual(out({"id": 1, "result": {"resultType": "input_required", "inputRequests": {}}}), "input_required")
        self.assertEqual(out({"id": 1, "error": {"code": -32602, "message": "x"}}), "error")
        self.assertEqual(out({"id": 1, "result": {"task": {"taskId": "t1"}}}), "other")                # a handle, not a result
        self.assertEqual(out({"id": 1, "result": {"resultType": "something_new"}}), "other")
        self.assertEqual(out({"id": 1, "result": "odd"}), "other")

    def test_a_blocked_call_is_a_tool_error_in_the_dialect_of_the_caller(self):
        legacy = rpc.tool_error({"id": 7, "method": "tools/call", "params": {"name": "buy"}}, "no")
        self.assertEqual(legacy, {"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": "no"}], "isError": True}})
        modern = rpc.tool_error({"id": "x", "method": "tools/call", "params": {"name": "buy", "_meta": {rpc.MODERN_META: "2026-07-28"}}}, "no")
        self.assertEqual(modern["result"]["resultType"], "complete")
        self.assertEqual(modern["id"], "x")


class ExtractionTests(unittest.TestCase):
    def test_dig(self):
        value = {"a": {"b": [{"c": 5}, {"c": 6}]}, "k-1": 1}
        self.assertEqual(dig(value, "a.b[1].c"), 6)
        self.assertEqual(dig(value, "k-1"), 1)
        missing = dig({}, "nope")
        for bad in ("a.x", "a.b[2].c", "a.b.c", "a..b", "z", "a.b[x]"):
            self.assertIs(dig(value, bad), missing, bad)

    def test_amounts(self):
        self.assertEqual(to_amount(19.99, None), 19.99)
        self.assertEqual(to_amount("$1,299.50", None), 1299.5)
        self.assertEqual(to_amount(1999, Decimal("0.01")), 19.99)             # cents, with no floating point noise
        self.assertEqual(to_amount("2500", Decimal("0.01")), 25.0)
        for bad in (True, None, "abc", "", -1, "-5", float("nan"), float("inf")):
            with self.assertRaises(ValueError, msg=repr(bad)):
                to_amount(bad, None)

    def test_text(self):
        self.assertEqual(to_text("  shoes "), "shoes")
        self.assertEqual(to_text(["a", {"name": "b"}, {"title": "c"}, {"x": 1}]), "a, b, c")
        with self.assertRaises(ValueError):
            to_text(5)

    def test_explicit_mapping(self):
        spec = ToolSpec.from_dict("buy", {"merchant": "store.id", "amount": "total", "amount_scale": 0.01, "description": "items", "category": {"const": "sports"}})
        fields, problems = extract(spec, {"store": {"id": "stridemart"}, "total": 8999, "items": [{"name": "road shoes"}]}, None)
        self.assertEqual(problems, [])
        self.assertEqual(fields, {"merchant": "stridemart", "amount": 89.99, "description": "road shoes", "category": "sports"})

    def test_a_value_can_come_from_the_result(self):
        spec = ToolSpec.from_dict("checkout", {"merchant": "merchant", "amount": "result.total"})
        fields, problems = extract(spec, {"merchant": "stridemart"}, None)
        self.assertEqual(fields, {"merchant": "stridemart"})
        self.assertTrue(any(p.startswith("amount") for p in problems))              # once the call has run, it is missing
        fields, problems = extract(spec, {"merchant": "stridemart"}, None, final=False)
        self.assertEqual((fields, problems), ({"merchant": "stridemart"}, []))      # before it runs, it is simply not there yet
        fields, problems = extract(spec, {"merchant": "stridemart"}, {"total": 42.5})
        self.assertEqual((fields["amount"], problems), (42.5, []))

    def test_missing_and_bad_values_are_reported_by_name(self):
        spec = ToolSpec.from_dict("buy", {"merchant": "merchant", "amount": "price"})
        fields, problems = extract(spec, {"merchant": "x", "price": "free"}, None)
        self.assertEqual(fields, {"merchant": "x"})
        self.assertEqual(len(problems), 1)
        self.assertIn("amount", problems[0])
        _, problems = extract(spec, {}, None)
        self.assertEqual(len(problems), 2)

    def test_automatic_mapping_guesses_common_names(self):
        auto = ToolSpec()
        fields, problems = extract(auto, {"merchant_id": "stridemart", "price": 12.5, "item": "shoes", "category": "sports"}, None)
        self.assertEqual((fields, problems), ({"merchant": "stridemart", "amount": 12.5, "description": "shoes", "category": "sports"}, []))
        fields, _ = extract(auto, {"seller": "s", "total_cents": 1999}, None)
        self.assertEqual((fields["merchant"], fields["amount"]), ("s", 19.99))
        _, problems = extract(auto, {"query": "shoes"}, None)
        self.assertEqual(len(problems), 2)

    def test_which_tools_are_witnessed(self):
        cfg = config_from_dict({"tools": {"buy_item": {"merchant": "m", "amount": "a"}, "pay_*": {"merchant": "m", "amount": "a"}}, "watch": ["checkout*"]})
        self.assertFalse(cfg.spec_for("buy_item").auto)
        self.assertFalse(cfg.spec_for("pay_invoice").auto)
        self.assertTrue(cfg.spec_for("checkout_now").auto)
        self.assertIsNone(cfg.spec_for("search_products"))

    def test_settings_are_validated(self):
        ok = {"tools": {"buy": {"merchant": "m", "amount": "a"}}}
        config_from_dict(ok).validate()
        for bad, text in (({"nope": 1}, "unknown setting"), ({"tools": {"buy": {"amount": "a"}}}, "needs a merchant"),
                          ({"tools": {"buy": {"merchant": "m", "amount": "a", "bogus": 1}}}, "unknown setting"),
                          ({"tools": {"buy": {"merchant": "m", "amount": "a", "amount_scale": "x"}}}, "scale"), ([], "JSON object")):
            with self.assertRaisesRegex(ConfigError, text):
                config_from_dict(bad)
        for patch, text in (({"enforce": "maybe"}, "enforce"), ({"on_witness_down": "x"}, "on_witness_down"),
                            ({"mandate": {"file": "a", "id": "b"}}, "only one"), ({"witness": {"url": "ftp://x"}}, "http"),
                            ({"principal_key": "abc"}, "64"), ({"tools": {}}, "nothing to witness")):
            with self.assertRaisesRegex(ConfigError, text):
                config_from_dict({**ok, **patch}).validate()


def gateway(constraints, now=NOW, merchants=None):
    signed, principal = quick_mandate(intent="buy running shoes under $120", merchants=merchants, max_per_txn=constraints.get("max_per_txn"),
                                      max_total=constraints.get("max_total"), categories=constraints.get("categories"),
                                      step_up_above=constraints.get("step_up_above"), now=now, hours=24)
    return Gateway(signed, expected_principal_pub=principal.public_hex, clock=lambda: now)


class PolicyTests(unittest.TestCase):
    KINDS = {"above the per-purchase limit": "per_txn_cap", "total limit": "cumulative_cap", "not on the user's list": "merchant_allowed",
             "deny list": "merchant_not_denied", "category": "category_allowed", "confirmation": "step_up",
             "not valid at this time": "time_window", "does not allow": "action_type"}

    def kinds(self, decision):
        found = set()
        for violation in decision.violations:
            found |= {kind for text, kind in self.KINDS.items() if text in violation}
        return found

    def test_agrees_with_the_adjudicator_on_random_purchases(self):
        rng = random.Random(7)
        compared = blocked = 0
        for _ in range(400):
            constraints = {"max_per_txn": rng.choice([None, 50, 120.5]), "max_total": rng.choice([None, 150, 400]),
                           "categories": rng.choice([None, ["sports"]]), "step_up_above": rng.choice([None, 80])}
            merchants = rng.choice([None, ["stridemart"]])
            gw = gateway(constraints, merchants=merchants)
            for _ in range(rng.randint(0, 3)):
                gw.record(merchant="stridemart", amount=rng.choice([10, 30, 60]), description="road running shoes", category="sports")
            prior = sum(r.action.amount for r in gw.receipts)
            merchant, amount = rng.choice(["stridemart", "shadymart"]), rng.choice([10.0, 60.0, 90.0, 130.0, 250.0])
            category = rng.choice(["sports", "food", "general"])
            when = rng.choice([NOW, NOW, NOW, NOW + 90_000])         # sometimes after the mandate has expired
            gw._clock = lambda t=when: t
            seq = gw.record(merchant=merchant, amount=amount, description="road running shoes", category=category).seq
            verdict = gw.dispute(seq).verdict
            adjudicated = {c.name for c in verdict.checks if not c.passed and c.name in {"per_txn_cap", "cumulative_cap", "merchant_allowed",
                           "merchant_not_denied", "category_allowed", "step_up", "time_window", "action_type"}}
            decision = Policy(gw.mandate, spent=prior).check(merchant, amount, when, category=category)
            self.assertEqual(self.kinds(decision), adjudicated, (constraints, merchants, merchant, amount, category, when, prior))
            self.assertEqual(decision.allowed, not adjudicated)
            compared += 1
            blocked += not decision.allowed
        self.assertEqual(compared, 400)
        self.assertTrue(80 < blocked < 360, blocked)              # the cases really did exercise both outcomes

    def test_the_deny_list_and_actions_and_currency(self):
        signer = Signer()
        m = Mandate("m-d", "u", "agent-1", NOW - 60, NOW + 3600, ("purchase",), Constraints(merchants_deny=("badshop",), currency="USD"), "buy shoes")
        policy = Policy(m)
        self.assertFalse(policy.check("badshop", 10, NOW).allowed)
        self.assertTrue(policy.check("goodshop", 10, NOW).allowed)
        self.assertFalse(policy.check("goodshop", 10, NOW, action="refund").allowed)
        self.assertFalse(policy.check("goodshop", 10, NOW, currency="EUR").allowed)
        self.assertFalse(policy.check("goodshop", 10, NOW - 3600).allowed)

    def test_reservations_stop_two_purchases_slipping_under_the_total_together(self):
        m = Mandate("m-t", "u", "agent-1", NOW - 60, NOW + 3600, ("purchase",), Constraints(max_total=100.0), "buy shoes")
        policy = Policy(m)
        self.assertTrue(policy.check("s", 60, NOW, reserve=True).allowed)
        self.assertFalse(policy.check("s", 60, NOW, reserve=True).allowed)      # 60 is already held
        policy.release(60)
        self.assertTrue(policy.check("s", 60, NOW, reserve=True).allowed)
        policy.commit(60)
        self.assertEqual(policy.spent, 60)
        self.assertFalse(policy.check("s", 60, NOW).allowed)
        self.assertTrue(policy.check("s", 40, NOW).allowed)

    def test_reservations_hold_under_threads(self):
        m = Mandate("m-t", "u", "agent-1", NOW - 60, NOW + 3600, ("purchase",), Constraints(max_total=100.0), "buy shoes")
        policy = Policy(m)
        allowed = []

        def attempt():
            allowed.append(policy.check("s", 10, NOW, reserve=True).allowed)

        threads = [threading.Thread(target=attempt) for _ in range(40)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sum(allowed), 10)

    def test_strict_mode_also_wants_the_item_to_match_the_intent(self):
        m = Mandate("m-s", "u", "agent-1", NOW - 60, NOW + 3600, ("purchase",), Constraints(), "buy running shoes under $120")
        strict = Policy(m, mode="strict")
        self.assertTrue(strict.check("s", 90, NOW, description="road running shoes").allowed)
        no = strict.check("s", 90, NOW, description="gaming console")
        self.assertFalse(no.allowed)
        self.assertIn("match", no.explain())
        self.assertTrue(Policy(m, mode="hard").check("s", 90, NOW, description="gaming console").allowed)


class AuditTests(unittest.TestCase):
    def test_records_survive_and_damage_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "audit.jsonl")
            log = AuditLog(path, clock=lambda: 12.3456)
            log.event("forwarded", call_id="a", tool="buy")
            log.event("recorded", call_id="a", seq=0)
            log.close()
            with open(path, "ab") as f:
                f.write(b"not json\n")
            log = AuditLog(path)
            log.event("blocked", call_id="b")
            log.close()
            events = AuditLog.read(path)
            self.assertEqual([e["event"] for e in events], ["forwarded", "recorded", "blocked"])
            self.assertEqual(events[0]["ts"], 12.346)
            self.assertEqual(AuditLog.read(os.path.join(d, "missing.jsonl")), [])

    def test_threads_do_not_interleave_lines(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "audit.jsonl")
            log = AuditLog(path)
            threads = [threading.Thread(target=lambda i=i: [log.event("e", n=i, pad="x" * 200) for _ in range(25)]) for i in range(8)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            log.close()
            self.assertEqual(len(AuditLog.read(path)), 200)


if __name__ == "__main__":
    unittest.main()
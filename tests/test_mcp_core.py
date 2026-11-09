import os
import tempfile
import unittest

from evenhand.mcp import rpc
from evenhand.mcp.audit import AuditLog
from evenhand.mcp.config import ConfigError, config_from_dict
from evenhand.mcp.core import PENDING_TTL, Engine
from evenhand.mcp.witness import EmbeddedWitness, open_mandate
from tests.mcp_helpers import MAPPING, NOW, Clock, FakeWitness, answer, buy, call, mandate_info


class Base(unittest.TestCase):
    enforce = "off"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.clock = Clock()
        self.witness = FakeWitness()
        self.logs = []

    def engine(self, enforce=None, spent=0.0, tools=None, down="block", **overrides):
        cfg = config_from_dict({"tools": tools if tools is not None else {"buy_item": MAPPING}, "enforce": self.enforce if enforce is None else enforce,
                                "on_witness_down": down})
        self.audit = AuditLog(os.path.join(self.dir, "audit.jsonl"), clock=self.clock)
        self.addCleanup(self.audit.close)
        return Engine(cfg, self.witness, mandate_info(spent, **overrides), self.audit, log=self.logs.append, clock=self.clock)

    def events(self, kind=None):
        records = AuditLog.read(os.path.join(self.dir, "audit.jsonl"))
        return [r for r in records if kind is None or r["event"] == kind]


class WitnessingTests(Base):
    def test_other_traffic_is_left_alone(self):
        engine, pending = self.engine(), {}
        for message in ({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, call(2, "search_products", query="shoes"),
                        {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}}):
            self.assertIsNone(engine.screen(message, pending))
        self.assertEqual(pending, {})
        self.assertEqual(self.events(), [])

    def test_a_successful_purchase_is_recorded_before_the_answer_is_released(self):
        engine, pending = self.engine(), {}
        self.assertIsNone(engine.screen(buy(7), pending))
        self.assertEqual(len(pending), 1)
        engine.observe(answer(7), pending)
        self.assertEqual(pending, {})
        self.assertEqual(self.witness.recorded, [("m-core", {"merchant": "stridemart", "amount": 89.0, "description": "road running shoes", "category": "sports"})])
        recorded = self.events("recorded")[0]
        self.assertEqual((recorded["seq"], recorded["receipt_hash"], recorded["amount"]), (0, "hash0", 89.0))
        self.assertEqual(engine.policy.spent, 89.0)

    def test_failures_and_unfinished_results_are_not_purchases(self):
        engine, pending = self.engine(), {}
        for n, result in enumerate((answer(0, isError=True), {"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad"}},
                                    answer(2, resultType="input_required", inputRequests={}), answer(3, task={"taskId": "t"}))):
            engine.screen(buy(n), pending)
            engine.observe(result if "id" in result and result["id"] == n else {**result, "id": n}, pending)
        self.assertEqual(self.witness.recorded, [])
        self.assertEqual(pending, {})
        self.assertEqual([e["event"] for e in self.events() if e["event"] != "forwarded"], ["tool_error", "error", "input_required", "other"])
        self.assertEqual(engine.policy.spent, 0.0)

    def test_answers_that_belong_to_nothing_pass_quietly(self):
        engine, pending = self.engine(), {}
        engine.observe(answer(99), pending)
        engine.observe({"jsonrpc": "2.0", "method": "notifications/progress"}, pending)
        engine.observe("not a message", pending)
        self.assertEqual(self.witness.recorded, [])

    def test_the_description_defaults_to_the_tool_name(self):
        engine, pending = self.engine(tools={"pay": {"merchant": "to", "amount": "amount"}}), {}
        engine.screen(call(1, "pay", to="stridemart", amount="$25"), pending)
        engine.observe(answer(1), pending)
        self.assertEqual(self.witness.recorded[0][1], {"merchant": "stridemart", "amount": 25.0, "description": "pay"})

    def test_amounts_can_come_from_the_result(self):
        engine, pending = self.engine(tools={"checkout": {"merchant": "store", "amount": "result.structuredContent.total", "amount_scale": 0.01}}), {}
        self.assertIsNone(engine.screen(call(1, "checkout", store="stridemart"), pending))
        engine.observe(answer(1, structuredContent={"total": 4250}), pending)
        self.assertEqual(self.witness.recorded[0][1]["amount"], 42.5)

    def test_a_result_that_lacks_the_amount_is_reported_not_recorded(self):
        engine, pending = self.engine(tools={"checkout": {"merchant": "store", "amount": "result.total"}}), {}
        engine.screen(call(1, "checkout", store="stridemart"), pending)
        engine.observe(answer(1), pending)
        self.assertEqual(self.witness.recorded, [])
        self.assertEqual(self.events("unrecorded")[0]["tool"], "checkout")
        self.assertTrue(any("could not be recorded" in line for line in self.logs))

    def test_calls_that_cannot_be_read_are_passed_but_flagged_when_nothing_is_enforced(self):
        engine, pending = self.engine(), {}
        self.assertIsNone(engine.screen(call(1, merchant="stridemart", item="shoes"), pending))        # no price
        self.assertEqual(pending, {})
        self.assertEqual(self.events("unrecorded")[0]["tool"], "buy_item")

    def test_a_reused_request_id_does_not_lose_track(self):
        engine, pending = self.engine("hard"), {}
        engine.screen(buy(1, price=50), pending)
        engine.screen(buy(1, price=60), pending)
        self.assertEqual(len(pending), 1)
        self.assertEqual(engine.policy.check("stridemart", 90, NOW, category="sports", reserve=False).allowed, True)   # only 60 is held
        self.assertEqual(len(self.events("interrupted")), 1)

    def test_a_crash_while_recording_does_not_stop_the_answer(self):
        engine, pending = self.engine(), {}
        engine.screen(buy(1), pending)
        self.witness.record = lambda *a: (_ for _ in ()).throw(TypeError("boom"))
        engine.observe(answer(1), pending)
        self.assertEqual(pending, {})
        self.assertIn("internal error", self.events("interrupted")[0]["reason"])


class EnforcementTests(Base):
    enforce = "hard"

    def test_a_purchase_within_the_mandate_goes_through_and_is_held_against_the_total(self):
        engine, pending = self.engine(), {}
        self.assertIsNone(engine.screen(buy(1, price=89), pending))
        self.assertEqual(engine.policy.check("stridemart", 120, NOW, category="sports").allowed, False)      # 89 held, 200 total
        engine.observe(answer(1), pending)
        self.assertEqual(engine.policy.spent, 89.0)

    def test_a_purchase_outside_the_mandate_is_blocked_and_the_agent_is_told(self):
        engine, pending = self.engine(), {}
        reply = rpc.parse(engine.screen(buy(5, merchant="shadymart"), pending))
        self.assertEqual(reply["id"], 5)
        self.assertTrue(reply["result"]["isError"])
        text = reply["result"]["content"][0]["text"]
        self.assertIn("blocked", text)
        self.assertIn("shadymart", text)
        self.assertIn("Nothing was bought", text)
        self.assertEqual(pending, {})
        blocked = self.events("blocked")[0]
        self.assertIn("not on the user's list", blocked["reason"])
        self.assertEqual(self.events("forwarded"), [])

    def test_each_hard_limit_blocks(self):
        for tweak, expect in ((dict(price=150), "per-purchase limit"), (dict(category="food"), "category"), (dict(merchant="x"), "user's list")):
            engine, pending = self.engine(), {}
            reply = rpc.parse(engine.screen(buy(1, **tweak), pending))
            self.assertIn(expect, reply["result"]["content"][0]["text"], tweak)
        engine = self.engine(spent=150)
        self.assertIn("total limit", rpc.parse(engine.screen(buy(1, price=80), {}))["result"]["content"][0]["text"])
        engine = self.engine(step_up_above=50)
        self.assertIn("confirmation", rpc.parse(engine.screen(buy(1, price=80), {}))["result"]["content"][0]["text"])
        self.clock.now = NOW + 90_000
        engine = self.engine()
        self.assertIn("not valid at this time", rpc.parse(engine.screen(buy(1), {}))["result"]["content"][0]["text"])

    def test_a_blocked_answer_is_in_the_dialect_of_the_caller(self):
        engine = self.engine()
        modern = buy(1, merchant="x")
        modern["params"]["_meta"] = {rpc.MODERN_META: "2026-07-28"}
        self.assertEqual(rpc.parse(engine.screen(modern, {}))["result"]["resultType"], "complete")
        self.assertNotIn("resultType", rpc.parse(engine.screen(buy(2, merchant="x"), {}))["result"])

    def test_a_call_that_cannot_be_read_is_blocked_not_guessed_at(self):
        engine, pending = self.engine(), {}
        reply = rpc.parse(engine.screen(call(1, merchant="stridemart"), pending))
        self.assertIn("could not read", reply["result"]["content"][0]["text"])
        self.assertEqual(pending, {})

    def test_enforcement_needs_the_amount_before_the_call_runs(self):
        with self.assertRaisesRegex(ConfigError, "before the call runs"):
            self.engine(tools={"checkout": {"merchant": "store", "amount": "result.total"}})

    def test_strict_mode_also_blocks_an_item_that_is_not_what_was_asked_for(self):
        engine = self.engine("strict")
        self.assertIsNone(engine.screen(buy(1, item="road running shoes"), {}))
        reply = rpc.parse(engine.screen(buy(2, item="gaming console"), {}))
        self.assertIn("match", reply["result"]["content"][0]["text"])

    def test_two_purchases_at_once_cannot_both_slip_under_the_total(self):
        engine, pending = self.engine(spent=0), {}
        self.assertIsNone(engine.screen(buy(1, price=110), pending))
        self.assertIsNotNone(engine.screen(buy(2, price=110), pending))       # 110 is already held; 220 would exceed 200
        engine.observe(answer(1, isError=True), pending)                     # the first one failed, so its hold is released
        self.assertIsNone(engine.screen(buy(3, price=110), pending))

    def test_a_blocked_batch_runs_nothing(self):
        engine, pending = self.engine(), {}
        batch = [buy(1), buy(2, merchant="shadymart"), {"jsonrpc": "2.0", "id": 3, "method": "tools/list"}]
        replies = rpc.parse(engine.screen(batch, pending))
        self.assertEqual([r["id"] for r in replies], [1, 2, 3])
        self.assertIn("another purchase in the same batch", replies[0]["result"]["content"][0]["text"])      # fine itself, but not run
        self.assertIn("shadymart", replies[1]["result"]["content"][0]["text"])                              # the one that was blocked
        self.assertEqual(replies[2]["error"]["code"], -32600)
        self.assertEqual(pending, {})
        self.assertEqual(engine.policy.check("stridemart", 120, NOW, category="sports").allowed, True)           # nothing is held
        ok = [buy(1), buy(2)]
        self.assertIsNone(engine.screen(ok, pending))
        self.assertEqual(len(pending), 2)

    def test_a_call_that_is_never_answered_lets_go_of_its_hold(self):
        engine, pending = self.engine(), {}
        self.assertIsNone(engine.screen(buy(1, price=110), pending))
        self.assertIsNotNone(engine.screen(buy(2, price=100), pending))        # 110 is held, so 210 would pass the 200 total
        self.clock.now += PENDING_TTL + 1
        self.assertIsNone(engine.screen(buy(3, price=100), pending))           # the unanswered call's hold has been let go
        self.assertEqual(len(self.events("interrupted")), 1)


class WitnessOutageTests(Base):
    def test_by_default_no_witness_means_no_purchase(self):
        engine, pending = self.engine(down="block"), {}
        self.witness.down = True
        reply = rpc.parse(engine.screen(buy(1), pending))
        self.assertIn("could not reach its witness", reply["result"]["content"][0]["text"])
        self.assertEqual(pending, {})
        self.assertIn("witness could not be reached", self.events("blocked")[0]["reason"])

    def test_the_health_check_is_remembered_for_a_moment(self):
        engine = self.engine(down="block")
        engine.screen(buy(1), {})
        engine.screen(buy(2), {})
        self.assertEqual(self.witness.checks, 1)
        self.clock.now += 3
        engine.screen(buy(3), {})
        self.assertEqual(self.witness.checks, 2)

    def test_queueing_lets_the_purchase_through_and_records_the_receipt_later(self):
        engine, pending = self.engine(down="queue"), {}
        self.witness.down = True
        self.assertIsNone(engine.screen(buy(1), pending))
        engine.observe(answer(1), pending)
        self.assertEqual((engine.queued, self.witness.recorded), (1, []))
        self.assertEqual(engine.policy.spent, 89.0)                                    # the money was spent either way
        self.assertEqual(self.events("queued")[0]["action"]["merchant"], "stridemart")
        self.assertEqual(engine.flush_queue(), 0)                                      # still down
        self.witness.down = False
        self.assertEqual(engine.flush_queue(), 1)
        self.assertEqual((engine.queued, len(self.witness.recorded)), (0, 1))
        self.assertTrue(self.events("recorded")[0]["retried"])

    def test_receipts_waiting_at_shutdown_are_found_again_on_the_next_start(self):
        engine, pending = self.engine(down="queue"), {}
        self.witness.down = True
        engine.screen(buy(1), pending)
        engine.observe(answer(1), pending)
        engine.screen(buy(2, price=20), pending)                      # in flight at shutdown
        engine.close(pending)
        self.assertTrue(any("could not be recorded" in line for line in self.logs))
        self.witness.down = False
        revived = self.engine(down="queue")
        self.assertEqual(revived.queued, 1)
        revived.start(retry_every=60)
        self.assertEqual([a["amount"] for _, a in self.witness.recorded], [89.0])
        self.assertEqual(revived.queued, 0)
        revived.close()
        self.assertEqual(sum(1 for e in self.events("recorded")), 1)

    def test_calls_in_flight_when_the_proxy_died_are_reported_once(self):
        engine, pending = self.engine(), {}
        engine.screen(buy(1), pending)                                # forwarded, then the process dies without close()
        self.audit.close()
        revived = self.engine()
        self.assertTrue(any("in flight" in line for line in self.logs))
        self.assertEqual(len(self.events("interrupted")), 1)
        self.audit.close()
        self.logs.clear()
        again = self.engine()
        self.assertFalse(any("in flight" in line for line in self.logs))
        self.assertEqual((revived.queued, again.queued), (0, 0))

    def test_closing_lets_go_of_what_is_still_pending(self):
        engine, pending = self.engine("hard"), {}
        engine.screen(buy(1, price=100), pending)
        engine.close(pending)
        self.assertEqual(pending, {})
        self.assertEqual(self.events("interrupted")[0]["reason"], "the proxy stopped before the call was answered")
        self.assertEqual(engine.policy.check("stridemart", 120, NOW, category="sports").allowed, True)


class WithARealWitnessTests(unittest.TestCase):
    def test_receipts_from_the_proxy_are_judged_by_the_real_adjudicator(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            embedded = EmbeddedWitness(os.path.join(d, "w"), dev=True)
            self.addCleanup(embedded.stop)
            info = open_mandate(embedded.client, dev={"intent": "buy running shoes under $120", "merchants": "stridemart", "max_per_txn": 120,
                                                     "categories": ["sports"], "mandate_id": "m-real"})
            cfg = config_from_dict({"tools": {"buy_item": MAPPING}})
            audit = AuditLog(os.path.join(d, "audit.jsonl"))
            self.addCleanup(audit.close)
            engine, pending = Engine(cfg, embedded.client, info, audit, log=lambda s: None), {}
            engine.screen(buy(1, price=89), pending)
            engine.observe(answer(1), pending)
            engine.screen(buy(2, merchant="shadymart", price=89), pending)        # nothing enforced: it goes through, and is witnessed
            engine.observe(answer(2), pending)
            chain = embedded.client.chain("m-real")
            self.assertEqual([r["action"]["merchant_id"] for r in chain["receipts"]], ["stridemart", "shadymart"])
            self.assertEqual(embedded.client.dispute("m-real", 0)["verdict"]["type"], "within_mandate")
            self.assertEqual(embedded.client.dispute("m-real", 1)["verdict"]["type"], "exceeded_mandate")
            self.assertEqual(open_mandate(embedded.client, mandate_id="m-real").spent, 178.0)


if __name__ == "__main__":
    unittest.main()
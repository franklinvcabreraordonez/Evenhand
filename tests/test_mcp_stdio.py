import json
import os
import random
import subprocess
import sys
import tempfile
import time
import unittest

from evenhand.mcp import rpc
from evenhand.mcp.audit import AuditLog
from tests.mcp_helpers import DEMO_SERVER, ECHO_SERVER, FakeWitness, Harness, buy, call, make_engine


class StdioBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.witness = FakeWitness()
        self.logs = []

    def proxy(self, argv=DEMO_SERVER, **kwargs):
        engine, audit = make_engine(self.dir, self.witness, logs=self.logs, **kwargs)
        self.addCleanup(audit.close)
        self.engine = engine
        return Harness(self, engine, argv)

    def events(self, kind=None):
        return [r for r in AuditLog.read(os.path.join(self.dir, "audit.jsonl")) if kind is None or r["event"] == kind]


class TransparencyTests(StdioBase):
    def test_everything_that_is_not_a_witnessed_call_arrives_byte_for_byte(self):
        rng = random.Random(3)
        lines = [b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}\n',
                 b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n',
                 b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":7}}\n',
                 b'{"jsonrpc":"2.0",  "id":2 ,"method":"tools/list"}\r\n',                      # odd spacing and a Windows line ending
                 '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"search_products","arguments":{"query":"caf\u00e9 \U0001f600"}}}\n'.encode("utf-8"),
                 b'not json at all\n', b'\n', b'\xff\xfe broken bytes\n', b'[1,2,3]\n',
                 b'[{"jsonrpc":"2.0","id":4,"method":"ping"},{"jsonrpc":"2.0","id":5,"method":"tools/list"}]\n',
                 b'{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"search_products","arguments":{"query":"' + b"x" * 300_000 + b'"}}}\n']
        lines += [json.dumps({"jsonrpc": "2.0", "id": 100 + i, "method": "x/y", "params": {"v": rng.random(), "s": "\u00e9" * rng.randint(0, 50)}}).encode("utf-8") + b"\n"
                  for i in range(40)]
        h = self.proxy(ECHO_SERVER)
        for line in lines:
            h.send_raw(line)
        received = [h.recv_raw() for _ in lines]
        self.assertEqual(received, lines)
        self.assertEqual(h.finish(), 0)
        self.assertEqual((self.witness.recorded, self.events()), ([], []))

    def test_a_proxied_server_answers_exactly_as_it_would_alone(self):
        requests = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    call(3, "search_products", query="shoes"), call(4, "no_such_tool"), {"jsonrpc": "2.0", "id": 5, "method": "nope"}]
        direct = subprocess.run(DEMO_SERVER, input=b"".join(json.dumps(r).encode() + b"\n" for r in requests), capture_output=True, timeout=30)
        h = self.proxy()
        for r in requests:
            h.send(r)
        via = [h.recv_raw() for _ in requests]
        self.assertEqual(b"".join(via), direct.stdout)
        h.finish()

    def test_the_servers_own_log_lines_come_through_on_stderr(self):
        h = self.proxy()
        h.request(buy(1))
        h.finish()
        time.sleep(0.2)
        self.assertTrue(any(b"demo-shop: sold road running shoes" in line for line in h.stderr), h.stderr)

    def test_a_last_line_without_a_newline_still_arrives(self):
        h = self.proxy(ECHO_SERVER)
        h.send_raw(b'{"jsonrpc":"2.0","id":9,"method":"ping"}')
        h.finish()
        self.assertEqual(h.recv_raw(), b'{"jsonrpc":"2.0","id":9,"method":"ping"}\n')


class WitnessingOverPipesTests(StdioBase):
    def test_a_purchase_is_recorded_and_the_agent_gets_the_real_result(self):
        h = self.proxy()
        h.request({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        reply = h.request(buy(2))
        self.assertEqual(reply["id"], 2)
        self.assertIn("Order ord-0001", reply["result"]["content"][0]["text"])
        self.assertEqual(self.witness.recorded, [("m-core", {"merchant": "stridemart", "amount": 89.0, "description": "road running shoes", "category": "sports"})])
        self.assertEqual(h.finish(), 0)

    def test_the_receipt_exists_before_the_agent_sees_the_result(self):
        self.witness.delay = 0.4
        h = self.proxy()
        h.send(buy(1))
        reply = h.recv()
        received = time.monotonic()
        self.assertEqual(reply["id"], 1)
        self.assertEqual(len(self.witness.recorded_at), 1)
        self.assertLessEqual(self.witness.recorded_at[0], received)

    def test_a_blocked_purchase_never_reaches_the_server(self):
        h = self.proxy(enforce="hard")
        bad = h.request(buy(1, merchant="shadymart"))
        self.assertTrue(bad["result"]["isError"])
        self.assertIn("not on the user's list", bad["result"]["content"][0]["text"])
        good = h.request(buy(2))
        self.assertIn("Order ord-0001", good["result"]["content"][0]["text"])           # the first order the shop ever took
        self.assertEqual(h.finish(), 0)
        self.assertEqual([a["merchant"] for _, a in self.witness.recorded], ["stridemart"])
        self.assertEqual(len(self.events("blocked")), 1)

    def test_failed_calls_and_unfinished_results_leave_no_receipt(self):
        h = self.proxy()
        self.assertTrue(h.request(buy(1, price=5000))["result"]["isError"])             # the shop declines it
        self.assertIn("error", h.request(call(2, "no_such_tool")))                       # a protocol error
        modern = buy(3)
        modern["method"] = "tools/call"
        modern["params"]["name"] = "buy_with_confirmation"
        modern["params"]["_meta"] = {rpc.MODERN_META: "2026-07-28"}
        self.assertEqual(h.request(modern)["result"]["resultType"], "input_required")   # not finished yet
        self.assertEqual(self.witness.recorded, [])
        retry = {**modern, "id": 4, "params": {**modern["params"], "inputResponses": {"confirm": {"action": "accept"}}}}
        self.assertEqual(h.request(retry)["result"]["resultType"], "complete")
        self.assertEqual(len(self.witness.recorded), 1)                                  # the finished call, once
        h.finish()

    def test_many_calls_at_once_are_all_answered_and_recorded(self):
        h = self.proxy()
        for n in range(30):
            h.send(buy(1000 + n, price=10 + n))
        replies = {h.recv()["id"] for _ in range(30)}
        self.assertEqual(replies, {1000 + n for n in range(30)})
        self.assertEqual(sorted(a["amount"] for _, a in self.witness.recorded), [10.0 + n for n in range(30)])
        h.finish()

    def test_a_cancelled_call_is_passed_on_and_still_witnessed_if_it_completes(self):
        h = self.proxy()
        h.send(call(1, "slow_buy", merchant="stridemart", price=40, item="road running shoes", category="sports", seconds=0.5))
        h.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}})
        self.assertEqual(h.recv()["id"], 1)
        self.assertEqual(len(self.witness.recorded), 1)
        h.finish()

    def test_with_the_witness_down_the_purchase_does_not_happen(self):
        self.witness.down = True
        h = self.proxy(down="block")
        reply = h.request(buy(1))
        self.assertIn("could not reach its witness", reply["result"]["content"][0]["text"])
        self.assertEqual(h.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["id"], 2)       # everything else carries on
        h.finish()
        self.assertEqual(self.events("forwarded"), [])

    def test_with_queueing_the_purchase_happens_and_the_receipt_follows(self):
        self.witness.down = True
        h = self.proxy(down="queue")
        self.assertIn("Order", h.request(buy(1))["result"]["content"][0]["text"])
        self.assertEqual(self.engine.queued, 1)
        self.witness.down = False
        self.assertEqual(self.engine.flush_queue(), 1)
        h.finish()


class EndingTests(StdioBase):
    def test_closing_the_input_ends_a_well_behaved_server(self):
        h = self.proxy()
        h.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        started = time.monotonic()
        self.assertEqual(h.finish(), 0)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_server_that_exits_ends_the_proxy_with_its_exit_code(self):
        h = self.proxy([sys.executable, "-c", "import sys; sys.stdout.write('{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":{}}\\n'); sys.stdout.flush(); sys.exit(3)"])
        self.assertEqual(h.recv()["id"], 1)
        self.assertEqual(h.finish(), 3)

    def test_calls_still_waiting_when_the_server_dies_are_recorded_as_interrupted(self):
        h = self.proxy([sys.executable, "-c", "import sys; sys.stdin.readline(); sys.exit(1)"], enforce="hard")
        h.send(buy(1))
        self.assertEqual(h.finish(), 1)
        self.assertEqual(len(self.events("interrupted")), 1)
        self.assertEqual(self.engine.policy.check("stridemart", 120, 1_800_000_000.0, category="sports").allowed, True)   # its hold was released

    def test_a_program_that_does_not_exist_is_reported(self):
        h = self.proxy(["definitely-not-a-real-program-xyz"])
        self.assertEqual(h.finish(), 127)
        self.assertTrue(any("cannot start" in line for line in self.logs))

    def test_a_server_that_ignores_the_close_is_stopped(self):
        sleeper = [sys.executable, "-c", "import time\ntime.sleep(60)"]
        from evenhand.mcp import stdio
        original = stdio._SHUTDOWN_GRACE
        stdio._SHUTDOWN_GRACE = 0.3
        self.addCleanup(setattr, stdio, "_SHUTDOWN_GRACE", original)
        h = self.proxy(sleeper)
        started = time.monotonic()
        h.finish()
        self.assertLess(time.monotonic() - started, 10)
        self.assertNotEqual(h.code, None)


if __name__ == "__main__":
    unittest.main()
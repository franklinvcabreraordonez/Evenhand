import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest

from evenhand.mcp import rpc
from evenhand.mcp.audit import AuditLog
from evenhand.mcp.httpproxy import Gateway
from tests.mcp_helpers import FakeWitness, buy, call, make_engine
from tests.mcp_http_fake import FakeHttpMcp


def post(gateway, message, headers=None, path="/mcp"):
    conn = http.client.HTTPConnection(*gateway.address, timeout=10)
    raw = message if isinstance(message, bytes) else json.dumps(message).encode()
    conn.request("POST", path, body=raw, headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})})
    response = conn.getresponse()
    body = response.read()
    status, got = response.status, {k.lower(): v for k, v in response.getheaders()}
    conn.close()
    return status, got, body


def sse_events(body):
    return [json.loads(line[5:]) for line in body.decode().splitlines() if line.startswith("data:")]


class GatewayBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.witness = FakeWitness()
        self.fake = FakeHttpMcp()
        self.addCleanup(self.fake.stop)
        self.logs = []

    def gateway(self, url=None, enforce="off", down="block", **kwargs):
        engine, audit = make_engine(self.dir, self.witness, enforce=enforce, down=down, logs=self.logs)
        self.addCleanup(audit.close)
        self.engine = engine
        gw = Gateway(engine, url or self.fake.url, port=0, **kwargs)
        gw.start()
        self.addCleanup(gw.stop)
        return gw

    def events(self, kind=None):
        return [r for r in AuditLog.read(os.path.join(self.dir, "audit.jsonl")) if kind is None or r["event"] == kind]

    def eventually(self, kind, count=1, seconds=5.0):
        """Some of what the gateway records happens just after it has answered, so wait for it rather than assume it is there."""
        deadline = time.time() + seconds
        while len(self.events(kind)) < count and time.time() < deadline:
            time.sleep(0.02)
        return self.events(kind)


class PassthroughTests(GatewayBase):
    def test_requests_and_responses_cross_unchanged(self):
        gw = self.gateway()
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        sent = {"Authorization": "Bearer abc", "Mcp-Session-Id": "sess-9", "MCP-Protocol-Version": "2025-11-25", "X-Custom": "yes", "Keep-Alive": "5"}
        status, headers, body = post(gw, message, sent)
        self.assertEqual((status, headers["content-type"]), (200, "application/json"))
        self.assertEqual(json.loads(body)["result"]["tools"][0]["name"], "buy_item")
        seen = self.fake.requests[-1]
        self.assertEqual(seen["body"], json.dumps(message).encode())
        for name in ("authorization", "mcp-session-id", "mcp-protocol-version", "x-custom"):
            self.assertEqual(seen["headers"][name], sent[{"authorization": "Authorization", "mcp-session-id": "Mcp-Session-Id", "mcp-protocol-version": "MCP-Protocol-Version",
                                                           "x-custom": "X-Custom"}[name]])
        self.assertNotIn("keep-alive", seen["headers"])
        self.assertEqual(seen["headers"]["host"], self.fake.url.split("/")[2])
        self.assertEqual(seen["headers"]["accept-encoding"], "identity")

    def test_a_session_started_by_the_server_works_through_the_gateway(self):
        self.fake.require_session = True
        gw = self.gateway()
        status, headers, _ = post(gw, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        self.assertEqual(headers["mcp-session-id"], "sess-1")
        self.assertEqual(post(gw, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})[0], 400)                      # no session, so the server refuses
        self.assertEqual(post(gw, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, {"Mcp-Session-Id": "sess-1"})[0], 200)
        self.assertEqual(post(gw, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"Mcp-Session-Id": "sess-1"})[0], 202)

    def test_the_newer_headers_are_left_exactly_as_the_client_set_them(self):
        gw = self.gateway()
        good = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_products", "arguments": {"query": "x"}}}
        ok = post(gw, good, {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": "search_products"})
        self.assertEqual(ok[0], 200)
        bad = post(gw, good, {"MCP-Protocol-Version": "2026-07-28", "Mcp-Method": "tools/call", "Mcp-Name": "something_else"})
        self.assertEqual(bad[0], 400)                                                                                # the server's own verdict, not altered
        self.assertEqual(json.loads(bad[2])["error"]["code"], -32020)

    def test_other_verbs_are_passed_on(self):
        gw = self.gateway()
        conn = http.client.HTTPConnection(*gw.address, timeout=10)
        conn.request("GET", "/mcp", headers={"Accept": "text/event-stream"})
        self.assertEqual(conn.getresponse().status, 405)
        conn.close()
        conn = http.client.HTTPConnection(*gw.address, timeout=10)
        conn.request("DELETE", "/mcp", headers={"Mcp-Session-Id": "sess-1"})
        self.assertEqual(conn.getresponse().status, 200)
        conn.close()
        self.assertEqual([r["method"] for r in self.fake.requests], ["GET", "DELETE"])
        self.assertEqual(self.fake.requests[1]["headers"]["mcp-session-id"], "sess-1")

    def test_a_server_stream_opened_with_get_is_passed_on(self):
        self.fake.legacy_get = True
        gw = self.gateway()
        conn = http.client.HTTPConnection(*gw.address, timeout=10)
        conn.request("GET", "/mcp", headers={"Accept": "text/event-stream"})
        response = conn.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        self.assertIn(b"notifications/message", response.read())
        conn.close()

    def test_a_chunked_request_and_a_large_one_arrive_whole(self):
        gw = self.gateway()
        big = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "search_products", "arguments": {"query": "x" * 3_000_000}}}
        self.assertEqual(post(gw, big)[0], 200)
        self.assertEqual(self.fake.requests[-1]["body"], json.dumps(big).encode())
        payload = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).encode()
        sock = socket.create_connection(gw.address, timeout=10)
        sock.sendall(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                     + f"{len(payload[:10]):x}\r\n".encode() + payload[:10] + b"\r\n" + f"{len(payload[10:]):x}\r\n".encode() + payload[10:] + b"\r\n0\r\n\r\n")
        data = b""
        while b"buy_item" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        sock.close()
        self.assertIn(b"200 OK", data)
        self.assertEqual(self.fake.requests[-1]["body"], payload)

    def test_the_wrong_path_and_a_foreign_origin_are_refused(self):
        gw = self.gateway()
        self.assertEqual(post(gw, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, path="/other")[0], 404)
        self.assertEqual(post(gw, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {"Origin": "https://evil.example"})[0], 403)
        self.assertEqual(post(gw, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {"Origin": "http://localhost:3000"})[0], 200)
        self.assertEqual(self.fake.requests[-1]["headers"].get("origin"), "http://localhost:3000")
        self.assertEqual(len(self.fake.requests), 1)                                                                  # the refused ones never got through
        allowed = self.gateway(allow_origins=["https://app.example"])
        self.assertEqual(post(allowed, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, {"Origin": "https://app.example"})[0], 200)

    def test_a_name_that_only_points_at_this_machine_is_refused(self):
        gw = self.gateway()
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        self.assertEqual(post(gw, message, {"Host": "evil.example"})[0], 403)                      # DNS rebinding
        self.assertEqual(post(gw, message, {"Host": "evil.example:8788"})[0], 403)
        self.assertEqual(post(gw, message, {"Host": "localhost:8788"})[0], 200)
        self.assertEqual(post(gw, message, {"Host": "[::1]:8788"})[0], 200)
        self.assertEqual(post(gw, message)[0], 200)                                                  # the address it is really on
        self.assertEqual(len(self.fake.requests), 3)
        named = self.gateway(allow_hosts=["agents.internal"])
        self.assertEqual(post(named, message, {"Host": "agents.internal:8788"})[0], 200)

    def test_extra_headers_can_be_added_for_the_upstream(self):
        gw = self.gateway(headers={"Authorization": "Bearer configured"})
        post(gw, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(self.fake.requests[-1]["headers"]["authorization"], "Bearer configured")


class WitnessingTests(GatewayBase):
    def test_a_streamed_result_is_recorded_before_its_event_is_released(self):
        self.witness.delay = 0.5                                              # a slow witness makes the order unmistakable
        gw = self.gateway()
        conn = http.client.HTTPConnection(*gw.address, timeout=10)
        conn.request("POST", "/mcp", body=json.dumps(buy(5)).encode(), headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
        response = conn.getresponse()
        self.assertEqual(response.getheader("Content-Type"), "text/event-stream")
        first = response.readline() + response.readline() + response.readline() + response.readline()
        self.assertIn(b"notifications/progress", first)
        self.assertEqual(len(self.witness.recorded), 0)                       # the progress event came through while the call was still running
        rest = b""
        while b"bought" not in rest:
            rest += response.readline()
        seen_at = time.monotonic()
        self.assertEqual(len(self.witness.recorded), 1)                       # and the receipt existed by the time the result arrived
        self.assertLessEqual(self.witness.recorded_at[0], seen_at)
        self.assertEqual(self.witness.recorded[0][1]["merchant"], "stridemart")
        conn.close()

    def test_streamed_events_arrive_byte_for_byte(self):
        gw = self.gateway()
        _, _, via = post(gw, buy(5))
        self.fake.progress_delay = 0.0
        direct = http.client.HTTPConnection("127.0.0.1", self.fake.server.server_address[1], timeout=10)
        direct.request("POST", "/mcp", body=json.dumps(buy(5)).encode(), headers={"Content-Type": "application/json"})
        self.assertEqual(via, direct.getresponse().read())

    def test_json_results_are_recorded_too_and_failures_are_not(self):
        gw = self.gateway()
        status, _, body = post(gw, call(1, "search_products", query="x"))
        self.assertEqual(json.loads(body)["result"]["content"][0]["text"], "found")
        self.assertEqual(self.witness.recorded, [])                                               # search is not watched
        engine, _ = self.engine, None
        engine.cfg.tools["fail_buy"] = engine.cfg.tools["buy_item"]
        post(gw, call(2, "fail_buy", merchant="stridemart", price=10, item="x", category="sports"))
        self.assertEqual(self.witness.recorded, [])                                               # the shop declined it
        self.assertEqual(self.events("tool_error")[0]["tool"], "fail_buy")

    def test_a_blocked_purchase_is_answered_by_the_gateway_and_never_sent_on(self):
        gw = self.gateway(enforce="hard")
        status, headers, body = post(gw, buy(1, merchant="shadymart"))
        reply = json.loads(body)
        self.assertEqual((status, headers["content-type"], reply["id"]), (200, "application/json", 1))
        self.assertTrue(reply["result"]["isError"])
        self.assertEqual(self.fake.requests, [])
        post(gw, buy(2))
        self.assertEqual(len(self.fake.requests), 1)
        self.assertEqual(len(self.witness.recorded), 1)

    def test_two_clients_using_the_same_request_id_are_not_confused(self):
        gw = self.gateway()
        self.fake.progress_delay = 0.2
        threads = [threading.Thread(target=post, args=(gw, buy(1, price=10 + n, item=f"item{n}"))) for n in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(a["amount"] for _, a in self.witness.recorded), [10.0 + n for n in range(6)])
        self.assertEqual(sorted(a["description"] for _, a in self.witness.recorded), [f"item{n}" for n in range(6)])

    def test_a_client_that_leaves_mid_stream_does_not_lose_the_receipt(self):
        gw = self.gateway(enforce="hard")
        self.fake.progress_delay = 0.4
        sock = socket.create_connection(gw.address, timeout=10)
        body = json.dumps(buy(1, price=100)).encode()
        sock.sendall(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        sock.recv(2048)          # the headers and the progress event
        sock.close()             # and then the client goes away
        deadline = time.time() + 5
        while not self.witness.recorded and time.time() < deadline:
            time.sleep(0.05)
        self.assertEqual(len(self.witness.recorded), 1)                      # the purchase happened, so it is on the record
        self.assertEqual(self.engine.policy.spent, 100.0)

    def test_an_unreachable_upstream_gives_a_bad_gateway_and_lets_go_of_the_hold(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        gw = self.gateway(url=f"http://127.0.0.1:{port}/mcp", enforce="hard", down="queue")
        status, _, body = post(gw, buy(1, price=100))
        self.assertEqual(status, 502)
        self.assertIn("could not be reached", json.loads(body)["error"]["message"])
        self.assertEqual(self.engine.policy.check("stridemart", 120, 1_800_000_000.0, category="sports").allowed, True)
        self.assertEqual(len(self.eventually("interrupted")), 1)

    def test_an_error_status_from_the_server_passes_through_and_records_nothing(self):
        gw = self.gateway()
        self.engine.cfg.tools["boom"] = self.engine.cfg.tools["buy_item"]
        status, _, _ = post(gw, call(1, "boom", merchant="stridemart", price=10, item="x", category="sports"))
        self.assertEqual(status, 500)
        self.assertEqual(self.witness.recorded, [])
        self.assertEqual(len(self.eventually("interrupted")), 1)

    def test_with_the_witness_down_the_gateway_refuses_the_purchase(self):
        self.witness.down = True
        gw = self.gateway(down="block")
        status, _, body = post(gw, buy(1))
        self.assertIn("could not reach its witness", json.loads(body)["result"]["content"][0]["text"])
        self.assertEqual(self.fake.requests, [])


if __name__ == "__main__":
    unittest.main()
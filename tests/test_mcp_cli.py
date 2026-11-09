import contextlib
import http.client
import io
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from evenhand.mcp import cli, demo
from evenhand.mcp.config import ConfigError
from evenhand.mcp.demo import PipeClient, handshake, proxy_argv, text_of
from evenhand.server import make_server
from tests.mcp_http_fake import FakeHttpMcp

DEV = ["--dev-mandate", "buy running shoes under $120", "--merchants", "stridemart", "--max-per-txn", "120", "--categories", "sports"]


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Folder(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def path(self, name):
        return os.path.join(self.dir, name)

    def session(self, *extra, steps=None):
        client = PipeClient(proxy_argv(self.path("data"), *extra))
        try:
            handshake(client)
            return [client.buy(*step) for step in (steps or [])], client
        finally:
            client.close()


class SettingsTests(unittest.TestCase):
    def test_a_tool_can_be_described_on_the_command_line(self):
        name, spec = cli.parse_tool("buy:merchant=store.id,amount=total,amount_scale=0.01,category=const:sports")
        self.assertEqual(name, "buy")
        self.assertEqual(sorted(spec.fields), ["amount", "category", "merchant"])
        self.assertEqual(spec.fields["category"].const, "sports")
        for bad in ("buy", "buy:", "buy:merchant=m", "buy:merchant=m,amount=a,colour=red", "buy:merchant=,amount=a"):
            with self.assertRaises(ConfigError, msg=bad):
                cli.parse_tool(bad)

    def test_flags_override_the_settings_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            with open(path, "w") as f:
                json.dump({"tools": {"buy_item": {"merchant": "m", "amount": "a"}}, "enforce": "hard", "data_dir": "from-file"}, f)
            args = cli.build_parser().parse_args(["run", "--config", path, "--enforce", "off", "--watch", "pay_*", "--tool", "other:merchant=m,amount=a", "--"])
            cfg = cli.build_config(args)
            self.assertEqual((cfg.enforce, cfg.data_dir, cfg.watch), ("off", "from-file", ["pay_*"]))
            self.assertEqual(sorted(cfg.tools), ["buy_item", "other"])

    def test_a_proxy_that_witnesses_nothing_is_refused(self):
        args = cli.build_parser().parse_args(["run", "--"])
        with self.assertRaisesRegex(ConfigError, "nothing to witness"):
            cli.build_config(args)


class KeyAndMandateTests(Folder):
    def test_a_key_and_a_signed_mandate_file(self):
        code, out, _ = run_cli("keygen", "--out", self.path("user.key"))
        self.assertEqual(code, 0)
        public = out.split("pin it when verifying): ")[1].strip()
        self.assertEqual(len(public), 64)
        with open(self.path("user.key")) as f:
            self.assertEqual(len(f.read().strip()), 64)
        code, _, err = run_cli("keygen", "--out", self.path("user.key"))
        self.assertEqual(code, 2)
        self.assertIn("never overwritten", err)
        code, out, _ = run_cli("mandate", "create", "--key", self.path("user.key"), "--intent", "buy running shoes", "--merchants", "stridemart,other",
                               "--max-per-txn", "120", "--mandate-id", "m-cli", "--out", self.path("mandate.json"))
        self.assertEqual(code, 0)
        self.assertIn(public, out)
        with open(self.path("mandate.json")) as f:
            data = json.load(f)
        self.assertEqual(data["principal_pub"], public)
        self.assertEqual(data["mandate"]["constraints"]["merchants_allow"], ["stridemart", "other"])
        code, _, err = run_cli("mandate", "create", "--key", self.path("user.key"), "--intent", "x", "--out", self.path("mandate.json"))
        self.assertEqual(code, 2)
        self.assertIn("already exists", err)
        code, _, err = run_cli("mandate", "create", "--key", self.path("missing.key"), "--intent", "x", "--out", self.path("m2.json"))
        self.assertEqual(code, 2)


class RunTests(Folder):
    def test_a_signed_mandate_file_end_to_end_with_pinned_keys(self):
        _, out, _ = run_cli("keygen", "--out", self.path("user.key"))
        public = out.split("pin it when verifying): ")[1].strip()
        run_cli("mandate", "create", "--key", self.path("user.key"), "--intent", "buy running shoes under $120", "--merchants", "stridemart", "--max-per-txn", "120",
                "--categories", "sports", "--mandate-id", "m-file", "--out", self.path("mandate.json"))
        replies, client = self.session("--mandate", self.path("mandate.json"), "--principal-key", public, "--enforce", "hard",
                                       steps=[("stridemart", "road running shoes", 89), ("shadymart", "road running shoes", 79)])
        self.assertIn("Order ord-0001", text_of(replies[0]))
        self.assertIn("not on the user's list", text_of(replies[1]))
        self.assertIn("mandate m-file", client.stderr_text)                           # the banner goes to stderr, never to the protocol channel
        code, out, _ = run_cli("report", "--data-dir", self.path("data"), "--user-key", public, "--disputes", "all", "--save-checkpoint", self.path("cp.json"))
        self.assertEqual(code, 0)
        self.assertIn("1 receipt(s)", out)
        self.assertIn("within_mandate", out)
        self.assertIn("the chain is intact", out)
        self.assertIn("1 blocked", out)

    def test_a_settings_file_runs_the_proxy(self):
        with open(self.path("c.json"), "w") as f:
            json.dump({"tools": {"buy_item": {"merchant": "merchant", "amount": "price", "description": "item"}}, "enforce": "hard"}, f)
        replies, _ = self.session("--config", self.path("c.json"), *DEV, steps=[("stridemart", "road running shoes", 89, "sports")])
        self.assertIn("Order", text_of(replies[0]))

    def test_the_witness_key_and_chain_persist_across_runs(self):
        self.session(*DEV, steps=[("stridemart", "road running shoes", 89)])
        first = run_cli("report", "--data-dir", self.path("data"))[1]
        replies, _ = self.session("--mandate-id", "m-dev-1", steps=[("stridemart", "road running shoes", 95)])
        self.assertIn("Order", text_of(replies[0]))
        second = run_cli("report", "--data-dir", self.path("data"))[1]
        self.assertIn("1 receipt(s)", first)
        self.assertIn("2 receipt(s)", second)

    def test_a_pinned_witness_key_that_does_not_match_stops_the_proxy(self):
        self.session(*DEV)
        client = PipeClient(proxy_argv(self.path("data"), "--mandate-id", "m-dev-1", "--witness-key", "00" * 32))
        self.assertEqual(client.close(), 2)
        self.assertIn("not the one you pinned", client.stderr_text)

    def refused(self, argv, expected):
        client = PipeClient([sys.executable, "-m", "evenhand.mcp", "run", *argv])
        self.assertEqual(client.close(), 2)
        self.assertIn(expected, client.stderr_text)
        self.assertIsNone(client._lines.get(timeout=5))                                     # nothing at all reached the protocol channel

    def test_problems_are_explained_and_nothing_stray_reaches_the_protocol_channel(self):
        nothing = ["--", sys.executable, "-c", "pass"]
        self.refused(["--data-dir", self.path("d1"), "--tool", demo.MAPPING, *DEV], "no tool server")
        self.refused(["--data-dir", self.path("d2"), *nothing], "nothing to witness")
        self.refused(["--data-dir", self.path("d3"), "--tool", demo.MAPPING, *nothing], "no mandate")
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        self.refused(["--data-dir", self.path("d4"), "--tool", demo.MAPPING, "--witness-url", f"http://127.0.0.1:{port}", "--mandate-id", "m", *nothing],
                     "could not reach the witness")

    def test_a_remote_witness_can_be_used(self):
        server = make_server(port=0, api_key="k", dev=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        url = f"http://127.0.0.1:{server.server_address[1]}"
        replies, _ = self.session("--witness-url", url, "--api-key", "k", *DEV, steps=[("stridemart", "road running shoes", 89)])
        self.assertIn("Order", text_of(replies[0]))
        code, out, _ = run_cli("report", "--witness-url", url, "--api-key", "k", "--mandate-id", "m-dev-1")
        self.assertEqual(code, 0)
        self.assertIn("1 receipt(s)", out)

    def test_killing_the_proxy_loses_nothing_that_was_recorded(self):
        client = PipeClient(proxy_argv(self.path("data"), *DEV))
        handshake(client)
        client.buy("stridemart", "road running shoes", 89)
        client.proc.kill()                                                                  # no goodbye
        client.close()
        code, out, _ = run_cli("report", "--data-dir", self.path("data"), "--mandate-id", "m-dev-1")
        self.assertEqual(code, 0)
        self.assertIn("1 receipt(s)", out)


class ReportTests(Folder):
    def test_a_cut_off_chain_is_caught_by_the_saved_checkpoint(self):
        self.session(*DEV, steps=[("stridemart", "road running shoes", 89), ("stridemart", "road running shoes", 95)])
        run_cli("report", "--data-dir", self.path("data"), "--save-checkpoint", self.path("cp.json"))
        self.session("--mandate-id", "m-dev-1", steps=[("stridemart", "road running shoes", 99)])
        log = self.path("data/witness/log.jsonl")
        with open(log, "rb") as f:
            lines = f.read().splitlines()
        receipts = [i for i, l in enumerate(lines) if b'"kind":"receipt"' in l]
        keep = [l for i, l in enumerate(lines) if i < receipts[1] and b'"kind":"checkpoint"' not in l]       # drop receipts 1 and 2, and the checkpoint records
        with open(log, "wb") as f:
            f.write(b"\n".join(keep) + b"\n")
        code, out, _ = run_cli("report", "--data-dir", self.path("data"), "--mandate-id", "m-dev-1")
        self.assertEqual((code, "the chain is intact" in out), (0, True))                            # on its own, the shortened record looks fine
        code, out, _ = run_cli("report", "--data-dir", self.path("data"), "--mandate-id", "m-dev-1", "--checkpoint-file", self.path("cp.json"))
        self.assertEqual(code, 1)
        self.assertIn("FAILED", out)
        self.assertIn("cut off", out)

    def test_a_record_that_was_edited_will_not_even_open(self):
        self.session(*DEV, steps=[("stridemart", "road running shoes", 89)])
        log = self.path("data/witness/log.jsonl")
        with open(log, "rb") as f:
            text = f.read().replace(b'"amount":89.0', b'"amount":1.0')
        with open(log, "wb") as f:
            f.write(text)
        code, _, err = run_cli("report", "--data-dir", self.path("data"), "--mandate-id", "m-dev-1")
        self.assertEqual(code, 2)
        self.assertIn("cannot make the report", err)

    def test_packets_are_written_for_each_verdict(self):
        self.session(*DEV, steps=[("stridemart", "road running shoes", 89), ("shadymart", "road running shoes", 79)])
        code, out, _ = run_cli("report", "--data-dir", self.path("data"), "--disputes", "1", "--packets", self.path("packets"))
        self.assertEqual(code, 0)
        self.assertIn("exceeded_mandate", out)
        names = os.listdir(self.path("packets"))
        self.assertTrue(any(n.endswith("exceeded_mandate.json") for n in names), names)
        code, _, err = run_cli("report", "--data-dir", self.path("data"), "--disputes", "9")
        self.assertEqual(code, 2)
        code, _, err = run_cli("report", "--data-dir", self.path("data"), "--disputes", "x")
        self.assertEqual(code, 2)

    def test_which_mandate_is_worked_out_from_the_proxys_own_log(self):
        code, _, err = run_cli("report", "--data-dir", self.path("empty"))
        self.assertEqual(code, 2)
        self.session(*DEV)
        self.assertEqual(run_cli("report", "--data-dir", self.path("data"))[0], 0)


class GatewayCommandTests(Folder):
    def test_the_gateway_command_witnesses_a_remote_server(self):
        fake = FakeHttpMcp()
        self.addCleanup(fake.stop)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        argv = [sys.executable, "-m", "evenhand.mcp", "serve-http", "--data-dir", self.path("data"), "--upstream-url", fake.url, "--listen", f"127.0.0.1:{port}",
                "--tool", demo.MAPPING, "--enforce", "hard", *DEV]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        lines = queue.Queue()
        threading.Thread(target=lambda: [lines.put(l) for l in iter(proc.stderr.readline, b"")], daemon=True).start()
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close(), proc.stderr.close()))
        seen = b""
        deadline = time.time() + 20
        while b"gateway listening" not in seen and time.time() < deadline:
            try:
                seen += lines.get(timeout=1)
            except queue.Empty:
                pass
        self.assertIn(b"gateway listening", seen, seen)

        def post(message):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
            conn.request("POST", "/mcp", body=json.dumps(message).encode(), headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
            body = conn.getresponse().read()
            conn.close()
            return body

        good = post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "buy_item", "arguments": {"merchant": "stridemart", "item": "x shoes", "price": 50, "category": "sports"}}})
        self.assertIn(b"bought x shoes", good)
        bad = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "buy_item", "arguments": {"merchant": "shadymart", "item": "x shoes", "price": 50, "category": "sports"}}})
        self.assertIn(b"not on the user", bad)
        self.assertEqual(len([r for r in fake.requests if b"tools/call" in r["body"]]), 1)
        proc.kill()                                                    # stop it the hard way: the record must already be safe
        proc.wait()
        code, out, _ = run_cli("report", "--data-dir", self.path("data"), "--mandate-id", "m-dev-1")
        self.assertEqual(code, 0)
        self.assertIn("1 receipt(s)", out)

    def test_a_bad_listen_address_or_header_is_reported(self):
        code, _, err = run_cli("serve-http", "--data-dir", self.path("d"), "--upstream-url", "http://127.0.0.1:1/mcp", "--tool", demo.MAPPING, *DEV, "--header", "oops")
        self.assertEqual(code, 2)
        code, _, err = run_cli("serve-http", "--data-dir", self.path("d2"), "--upstream-url", "notaurl", "--tool", demo.MAPPING, *DEV)
        self.assertEqual(code, 2)


class StoryTests(unittest.TestCase):
    def test_the_self_test_passes_here(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = demo.selftest()
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("All checks passed.", out.getvalue())

    def test_the_demo_tells_its_story(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = demo.demo()
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        for phrase in ("receipt 1 (shadymart, 79.00): exceeded_mandate", "Evenhand blocked this purchase", "it covered 3 receipts): ok", "1 blocked"):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
import asyncio
import dataclasses
import http.client
import json
import threading
import time
import unittest
import urllib.error
import urllib.request

from evenhand import Gateway, quick_mandate, verify_bundle
from evenhand.mandate import sign_revocation
from evenhand.receipts import verify_chain
from evenhand.serialize import (receipt_from_dict, receipt_to_dict, signed_mandate_from_dict,
                                signed_mandate_to_dict, revocation_to_dict)
from evenhand.mandate import verify_mandate
from evenhand.server import make_server


def new_gateway(**kw):
    signed, principal = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"],
                                      max_per_txn=120, max_total=300, categories=["sports"], **kw)
    return Gateway(signed, expected_principal_pub=principal.public_hex), signed, principal


class SerializeTests(unittest.TestCase):
    def test_signed_mandate_survives_json(self):
        _, signed, _ = new_gateway()
        again = signed_mandate_from_dict(json.loads(json.dumps(signed_mandate_to_dict(signed))))
        self.assertTrue(verify_mandate(again))
        self.assertEqual(again.digest, signed.digest)

    def test_missing_and_malformed_fields_are_named(self):
        _, signed, _ = new_gateway()
        d = signed_mandate_to_dict(signed)
        del d["mandate"]["constraints"]["step_up_above"]
        with self.assertRaisesRegex(ValueError, "step_up_above"):
            signed_mandate_from_dict(d)
        d = signed_mandate_to_dict(signed)
        d["signature"] = "abcd"
        with self.assertRaisesRegex(ValueError, "signature"):
            signed_mandate_from_dict(d)
        d = signed_mandate_to_dict(signed)
        d["mandate"]["issued_at"] = True  # a bool is not a number
        with self.assertRaisesRegex(ValueError, "issued_at"):
            signed_mandate_from_dict(d)

    def test_receipt_round_trip_keeps_the_hash(self):
        gw, _, _ = new_gateway()
        r = gw.record(merchant="stridemart", amount=50, description="road running shoes", category="sports")
        again = receipt_from_dict(json.loads(json.dumps(receipt_to_dict(r))))
        self.assertEqual(again.hash, r.hash)


class GatewayTests(unittest.TestCase):
    def test_tool_returns_the_result_and_records_one_receipt(self):
        gw, _, _ = new_gateway()

        @gw.tool(category="sports")
        def buy(merchant, amount, description):
            return f"ordered {description}"

        self.assertEqual(buy("stridemart", 99.0, "road running shoes"), "ordered road running shoes")
        self.assertEqual(len(gw.receipts), 1)
        a = gw.last_receipt.action
        self.assertEqual((a.merchant_id, a.amount, a.cart[0].desc, a.category), ("stridemart", 99.0, "road running shoes", "sports"))
        self.assertEqual(buy.__name__, "buy")  # functools.wraps keeps the name

    def test_verdicts_follow_the_mandate(self):
        gw, _, _ = new_gateway()

        @gw.tool(category="sports")
        def buy(merchant, amount, description):
            return True

        buy("stridemart", 99.0, "road running shoes")
        buy("shadymart", 89.0, "running shoes")
        self.assertEqual(gw.dispute(0).verdict.type.value, "within_mandate")
        d = gw.dispute()  # the latest by default
        self.assertEqual((d.seq, d.verdict.type.value), (1, "exceeded_mandate"))
        self.assertEqual(d.packet["verdict"], "exceeded_mandate")
        self.assertEqual(d.packet["receipts_supplied"], 2)

    def test_a_failed_call_is_not_witnessed(self):
        gw, _, _ = new_gateway()

        @gw.tool
        def buy(merchant, amount, description):
            raise RuntimeError("card declined")

        with self.assertRaises(RuntimeError):
            buy("stridemart", 10, "x")
        self.assertEqual(gw.receipts, [])

    def test_tools_without_merchant_and_amount_fail_fast(self):
        gw, _, _ = new_gateway()
        with self.assertRaises(TypeError):
            @gw.tool
            def buy(store, cost):
                pass

    def test_custom_extract(self):
        gw, _, _ = new_gateway()

        @gw.tool(extract=lambda a: {"merchant": a["store"], "amount": a["cart_total"], "description": a["note"]})
        def checkout(store, cart_total, note):
            return "ok"

        checkout("stridemart", 42.5, "road running shoes")
        self.assertEqual((gw.last_receipt.action.merchant_id, gw.last_receipt.action.amount), ("stridemart", 42.5))

    def test_async_tools(self):
        gw, _, _ = new_gateway()

        @gw.tool(category="sports")
        async def buy(merchant, amount, description):
            await asyncio.sleep(0)
            return "async ok"

        self.assertEqual(asyncio.run(buy("stridemart", 20, "road running shoes")), "async ok")
        self.assertEqual(len(gw.receipts), 1)

    def test_bad_amounts_are_rejected_and_nothing_is_recorded(self):
        gw, _, _ = new_gateway()
        for bad in (-1, float("nan"), float("inf"), True, "12"):
            with self.assertRaises(ValueError):
                gw.record(merchant="stridemart", amount=bad, description="x")
        with self.assertRaises(ValueError):
            gw.record(merchant=" ", amount=5, description="x")
        self.assertEqual(gw.receipts, [])

    def test_gateway_refuses_a_mandate_it_cannot_trust(self):
        signed, principal = quick_mandate(intent="buy running shoes under $120")
        forged = dataclasses.replace(signed, mandate=dataclasses.replace(signed.mandate, intent="buy anything"))
        with self.assertRaises(ValueError):
            Gateway(forged)
        with self.assertRaises(ValueError):
            Gateway(signed, expected_principal_pub="0" * 64)

    def test_many_threads_keep_the_chain_intact(self):
        gw, _, _ = new_gateway()

        @gw.tool(category="sports")
        def buy(merchant, amount, description):
            return True

        def work():
            for _ in range(25):
                buy("stridemart", 1.0, "road running shoes")

        threads = [threading.Thread(target=work) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        chain = gw.receipts
        self.assertEqual([r.seq for r in chain], list(range(200)))
        self.assertTrue(verify_chain(chain, gw.witness_pub, "m-001", gw.witness.head("m-001")).ok)

    def test_revocation_is_honored(self):
        gw, signed, principal = new_gateway()
        gw.record(merchant="stridemart", amount=40, description="road running shoes", category="sports")
        rev = sign_revocation(signed.mandate.mandate_id, time.time() - 30, principal)
        v = gw.dispute(revocations=[rev]).verdict
        self.assertEqual(v.type.value, "exceeded_mandate")
        self.assertIn("mandate revoked before action", v.reasons)


class BundleTests(unittest.TestCase):
    def setUp(self):
        self.gw, self.signed, self.principal = new_gateway()
        for amt in (10, 20, 30):
            self.gw.record(merchant="stridemart", amount=amt, description="road running shoes", category="sports")
        self.bundle = json.loads(json.dumps(self.gw.export()))

    def test_clean_bundle_verifies(self):
        out = verify_bundle(self.bundle, expected_principal_pub=self.principal.public_hex)
        self.assertEqual((out["ok"], out["receipts"]), (True, 3))

    def test_edited_amount_is_a_signature_failure(self):
        self.bundle["receipts"][1]["action"]["amount"] = 1.0
        out = verify_bundle(self.bundle)
        self.assertFalse(out["ok"])
        self.assertTrue(any("signature does not verify" in p for p in out["problems"]))
        self.assertTrue(any("stated hash" in p for p in out["problems"]))

    def test_deletion_and_truncation_are_caught(self):
        head = self.bundle["head"]
        gone = dict(self.bundle, receipts=self.bundle["receipts"][1:])
        self.assertFalse(verify_bundle(gone)["ok"])
        short = dict(self.bundle, receipts=self.bundle["receipts"][:-1], head=None)
        self.assertTrue(verify_bundle(short)["ok"])  # a shorter chain still verifies by itself...
        self.assertFalse(verify_bundle(short, expected_head=head)["ok"])  # ...unless you hold the witness checkpoint

    def test_wrong_principal_and_malformed_bundles(self):
        self.assertFalse(verify_bundle(self.bundle, expected_principal_pub="1" * 64)["ok"])
        out = verify_bundle({"format": "evenhand-bundle-v1"})
        self.assertFalse(out["ok"])
        self.assertIn("malformed", out["problems"][0])
        self.assertFalse(verify_bundle("not a bundle")["ok"])


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(port=0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.signed, cls.principal = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"],
                                                  max_per_txn=120, categories=["sports"], mandate_id="m-http")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method, path, body=None, token=None, raw=None, base=None):
        data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
        req = urllib.request.Request((base or self.base) + path, data=data, method=method, headers={"Content-Type": "application/json"})
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_full_flow_matches_the_python_gateway(self):
        s, out = self.call("POST", "/v1/mandates", signed_mandate_to_dict(self.signed))
        self.assertIn(s, (200, 201))
        self.assertEqual(self.call("POST", "/v1/mandates", signed_mandate_to_dict(self.signed))[0], 200)  # idempotent
        s, r0 = self.call("POST", "/v1/receipts", {"mandate_id": "m-http", "action": {
            "merchant": "stridemart", "amount": 99, "description": "road running shoes", "category": "sports", "ts": 1.0}})
        self.assertEqual((s, r0["seq"]), (201, 0))
        self.assertGreater(r0["action"]["ts"], 1_000_000_000)  # the witness's clock, not the client's
        s, r1 = self.call("POST", "/v1/receipts", {"mandate_id": "m-http", "action": {
            "merchant": "shadymart", "amount": 89, "description": "running shoes", "category": "sports"}})
        self.assertEqual(r1["seq"], 1)
        s, d0 = self.call("POST", "/v1/disputes", {"mandate_id": "m-http", "seq": 0})
        self.assertEqual((s, d0["verdict"]["type"], d0["status"]), (201, "within_mandate", "decided"))
        s, d1 = self.call("POST", "/v1/disputes", {"mandate_id": "m-http"})  # latest by default
        self.assertEqual((d1["seq"], d1["verdict"]["type"]), (1, "exceeded_mandate"))
        self.assertEqual(self.call("GET", f"/v1/disputes/{d1['dispute_id']}")[1]["verdict"]["type"], "exceeded_mandate")
        s, packet = self.call("GET", f"/v1/disputes/{d1['dispute_id']}/packet")
        self.assertEqual((s, packet["verdict"], packet["receipts_supplied"]), (200, "exceeded_mandate", 2))
        s, v = self.call("GET", "/v1/verify?mandate_id=m-http")
        self.assertEqual((s, v["ok"], v["receipts"]), (200, True, 2))
        s, bundle = self.call("GET", "/v1/chains/m-http")
        witness_pub = self.call("GET", "/healthz")[1]["witness_pub"]
        self.assertTrue(verify_bundle(bundle, expected_principal_pub=self.principal.public_hex, expected_witness_pub=witness_pub)["ok"])  # offline
        self.assertFalse(verify_bundle(bundle, expected_principal_pub=self.principal.public_hex, expected_witness_pub="00" * 32)["ok"])

    def test_errors_are_clear_and_safe(self):
        self.call("POST", "/v1/mandates", signed_mandate_to_dict(self.signed))
        self.assertEqual(self.call("POST", "/v1/receipts", {"mandate_id": "nope", "action": {}})[0], 404)
        self.assertEqual(self.call("POST", "/v1/receipts", raw=b"{not json")[0], 400)
        for bad in ({"merchant": "stridemart", "description": "x"}, {"merchant": "stridemart", "amount": -5, "description": "x"},
                    {"merchant": "stridemart", "amount": "12", "description": "x"}, {"amount": 5, "description": "x"},
                    {"merchant": "stridemart", "amount": 5}):
            self.assertEqual(self.call("POST", "/v1/receipts", {"mandate_id": "m-http", "action": bad})[0], 400, bad)
        forged = signed_mandate_to_dict(self.signed)
        forged["mandate"]["mandate_id"] = "m-forged"
        forged["mandate"]["intent"] = "buy anything"
        self.assertEqual(self.call("POST", "/v1/mandates", forged)[0], 422)
        clash = signed_mandate_to_dict(quick_mandate(intent="something else", mandate_id="m-http")[0])
        self.assertEqual(self.call("POST", "/v1/mandates", clash)[0], 409)
        self.assertEqual(self.call("GET", "/v1/disputes/d-9999")[0], 404)
        self.assertEqual(self.call("GET", "/v1/nothing")[0], 404)
        self.assertEqual(self.call("POST", "/v1/disputes", {"mandate_id": "m-http", "seq": "zero"})[0], 400)
        # an oversized request is refused from its headers alone, so the test never has to send a megabyte
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        conn.putrequest("POST", "/v1/mandates")
        conn.putheader("Content-Length", "1000001")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 413)
        conn.close()

    def test_bearer_token(self):
        server = make_server(port=0, api_key="s3cret")
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            self.assertEqual(self.call("GET", "/healthz", base=base)[0], 200)  # health stays open
            self.assertEqual(self.call("POST", "/v1/mandates", signed_mandate_to_dict(self.signed), base=base)[0], 401)
            self.assertEqual(self.call("POST", "/v1/mandates", signed_mandate_to_dict(self.signed), token="wrong", base=base)[0], 401)
            self.assertEqual(self.call("POST", "/v1/mandates", signed_mandate_to_dict(self.signed), token="s3cret", base=base)[0], 201)
        finally:
            server.shutdown()
            server.server_close()


def _call(base, method, path, body=None, token=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


class ServerSafetyTests(unittest.TestCase):
    def start(self, **kwargs):
        server = make_server(port=0, **kwargs)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        return server, f"http://127.0.0.1:{server.server_address[1]}"

    def test_allow_list_accepts_the_named_user_key_and_refuses_others(self):
        good, principal = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"], mandate_id="m-a")
        stranger, _ = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"], mandate_id="m-b")
        _, base = self.start(allowed_principals=[principal.public_hex])
        self.assertTrue(_call(base, "GET", "/healthz")[1]["principal_allowlist"])
        self.assertEqual(_call(base, "POST", "/v1/mandates", signed_mandate_to_dict(good))[0], 201)
        status, out = _call(base, "POST", "/v1/mandates", signed_mandate_to_dict(stranger))
        self.assertEqual(status, 403)
        self.assertIn("allow-list", out["error"])
        self.assertEqual(_call(base, "GET", "/v1/chains/m-b")[0], 404)  # it was never registered

    def test_dev_mode_cannot_be_combined_with_an_allow_list(self):
        with self.assertRaises(ValueError):
            make_server(port=0, dev=True, allowed_principals=["ab" * 32])

    def test_dev_helpers_are_off_by_default(self):
        _, base = self.start()
        self.assertFalse(_call(base, "GET", "/healthz")[1]["dev_mode"])
        self.assertEqual(_call(base, "POST", "/v1/dev/mandates", {"intent": "buy running shoes under $120"})[0], 404)
        self.assertEqual(_call(base, "POST", "/v1/dev/revocations", {"mandate_id": "m-dev"})[0], 404)

    def test_the_whole_flow_with_curl_style_calls_and_no_python_signing(self):
        _, base = self.start(dev=True)
        self.assertTrue(_call(base, "GET", "/healthz")[1]["dev_mode"])
        status, made = _call(base, "POST", "/v1/dev/mandates", {"intent": "buy running shoes under $120", "merchants": "stridemart",
                                                                "max_per_txn": 120, "categories": ["sports"], "mandate_id": "m-dev"})
        self.assertEqual(status, 201)
        self.assertIn("development only", made["warning"])
        sm = signed_mandate_from_dict(made["signed_mandate"])
        self.assertTrue(verify_mandate(sm))                      # a real signature, checkable offline
        self.assertEqual(sm.principal_pub, made["principal_pub"])
        for merchant, amount, item in (("stridemart", 99, "road running shoes"), ("shadymart", 89, "running shoes")):
            self.assertEqual(_call(base, "POST", "/v1/receipts", {"mandate_id": "m-dev", "action": {
                "merchant": merchant, "amount": amount, "description": item, "category": "sports"}})[0], 201)
        self.assertEqual(_call(base, "POST", "/v1/disputes", {"mandate_id": "m-dev", "seq": 0})[1]["verdict"]["type"], "within_mandate")
        self.assertEqual(_call(base, "POST", "/v1/disputes", {"mandate_id": "m-dev", "seq": 1})[1]["verdict"]["type"], "exceeded_mandate")
        witness_pub = _call(base, "GET", "/healthz")[1]["witness_pub"]
        bundle = _call(base, "GET", "/v1/chains/m-dev")[1]
        self.assertTrue(verify_bundle(bundle, expected_principal_pub=made["principal_pub"], expected_witness_pub=witness_pub)["ok"])
        # a revocation signed by the same throwaway user key, honored for a later action
        status, rev = _call(base, "POST", "/v1/dev/revocations", {"mandate_id": "m-dev", "revoked_at": 1.0})
        self.assertEqual(status, 201)
        self.assertEqual(_call(base, "POST", "/v1/receipts", {"mandate_id": "m-dev", "action": {
            "merchant": "stridemart", "amount": 50, "description": "road running shoes", "category": "sports"}})[1]["seq"], 2)
        verdict = _call(base, "POST", "/v1/disputes", {"mandate_id": "m-dev", "seq": 2, "revocations": [rev["revocation"]]})[1]["verdict"]
        self.assertEqual(verdict["type"], "exceeded_mandate")
        self.assertFalse([c for c in verdict["checks"] if c["name"] == "not_revoked"][0]["passed"])

    def test_dev_input_is_validated(self):
        _, base = self.start(dev=True)
        ok = {"intent": "buy running shoes under $120", "mandate_id": "m-x"}
        for bad in ({}, {"intent": ""}, {"intent": "x" * 501}, dict(ok, max_per_txn=-1), dict(ok, max_per_txn="12"), dict(ok, max_total=float("1e30")),
                    dict(ok, merchants=[1, 2]), dict(ok, merchants={"a": 1}), dict(ok, categories=[""]), dict(ok, hours=0), dict(ok, agent_id="a b"),
                    dict(ok, mandate_id="a/b"), dict(ok, mandate_id="x" * 129)):
            self.assertEqual(_call(base, "POST", "/v1/dev/mandates", bad)[0], 400, bad)
        self.assertEqual(_call(base, "POST", "/v1/dev/mandates", raw=b"[1, 2]")[0], 400)
        self.assertEqual(_call(base, "POST", "/v1/dev/mandates", ok)[0], 201)
        self.assertEqual(_call(base, "POST", "/v1/dev/mandates", ok)[0], 409)
        self.assertEqual(_call(base, "POST", "/v1/dev/revocations", {"mandate_id": "nope"})[0], 404)
        self.assertEqual(_call(base, "POST", "/v1/dev/revocations", {})[0], 400)
        self.assertEqual(_call(base, "POST", "/v1/dev/revocations", {"mandate_id": "m-x", "revoked_at": -5})[0], 400)

    def test_mandate_ids_must_be_url_safe(self):
        _, base = self.start()
        odd, _ = quick_mandate(intent="buy running shoes under $120", mandate_id="a/b")
        status, out = _call(base, "POST", "/v1/mandates", signed_mandate_to_dict(odd))
        self.assertEqual(status, 400)
        self.assertIn("mandate_id", out["error"])

    def test_the_bearer_token_also_protects_the_dev_helpers(self):
        _, base = self.start(dev=True, api_key="s3cret")
        body = {"intent": "buy running shoes under $120"}
        self.assertEqual(_call(base, "POST", "/v1/dev/mandates", body)[0], 401)
        self.assertEqual(_call(base, "POST", "/v1/dev/mandates", body, token="s3cret")[0], 201)

    def test_many_agents_at_once_keep_one_unbroken_chain(self):
        _, base = self.start(dev=True)
        _call(base, "POST", "/v1/dev/mandates", {"intent": "buy running shoes under $120", "merchants": "stridemart", "mandate_id": "m-load"})
        statuses, seqs, lock = [], [], threading.Lock()

        def agent(n):
            for i in range(12):
                st, r = _call(base, "POST", "/v1/receipts", {"mandate_id": "m-load", "action": {
                    "merchant": "stridemart", "amount": 10 + i, "description": f"road running shoes {n}-{i}", "category": "sports"}})
                with lock:
                    statuses.append(st)
                    seqs.append(r.get("seq"))

        threads = [threading.Thread(target=agent, args=(n,)) for n in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(set(statuses), {201})
        self.assertEqual(sorted(seqs), list(range(96)))          # every receipt got its own position
        out = _call(base, "GET", "/v1/verify?mandate_id=m-load")[1]
        self.assertEqual((out["ok"], out["receipts"]), (True, 96))


    def test_the_whole_request_body_is_read_before_any_reply_is_sent(self):
        # Replying early and closing with unread data makes Windows reset the connection, so the client never sees the
        # reply. The symptom depends on timing, so this checks the cause: the body is fully read before every reply.
        from evenhand import server as server_module
        seen = []
        original = server_module.Handler._send

        def spy(handler, status, payload):
            seen.append((status, int(handler.headers.get("Content-Length") or 0), len(getattr(handler, "_raw", b""))))
            return original(handler, status, payload)

        server_module.Handler._send = spy
        self.addCleanup(setattr, server_module.Handler, "_send", original)
        body = b'{"intent": "' + b"x" * 50_000 + b'"}'
        _, keyed = self.start(dev=True, api_key="s3cret")
        _, plain = self.start()
        for base, path in ((keyed, "/v1/dev/mandates"), (plain, "/v1/nothing"), (plain, "/v1/dev/mandates")):
            _call(base, "POST", path, raw=body)
        self.assertEqual([status for status, _, _ in seen], [401, 404, 404])
        for status, declared, read in seen:
            self.assertEqual(read, declared, f"a {status} reply was sent before the whole request body was read")


if __name__ == "__main__":
    unittest.main()

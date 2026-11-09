import json
import os
import socket
import tempfile
import threading
import unittest

from evenhand import quick_mandate
from evenhand.crypto import Signer
from evenhand.serialize import signed_mandate_to_dict
from evenhand.server import make_server
from evenhand.mcp.witness import EmbeddedWitness, WitnessClient, WitnessError, open_mandate

DEV = {"intent": "buy running shoes under $120", "merchants": "stridemart", "max_per_txn": 120, "categories": ["sports"], "mandate_id": "m-w"}


def action(merchant="stridemart", amount=30):
    return {"merchant": merchant, "amount": amount, "description": "road running shoes", "category": "sports"}


class ClientTests(unittest.TestCase):
    def test_talks_to_a_real_witness(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            embedded = EmbeddedWitness(d, dev=True)
            self.addCleanup(embedded.stop)
            c = embedded.client
            self.assertTrue(c.health()["persistent"])
            self.assertEqual(c.health()["witness_pub"], embedded.witness_pub)
            c.dev_mandate(DEV)
            receipt = c.record("m-w", action())
            self.assertEqual(receipt["seq"], 0)
            self.assertEqual(len(c.chain("m-w")["receipts"]), 1)
            cp = c.checkpoint("m-w")
            self.assertEqual(cp["count"], 1)
            d1 = c.dispute("m-w")
            self.assertEqual(d1["verdict"]["type"], "within_mandate")
            self.assertEqual(c.packet(d1["dispute_id"])["case_id"], d1["dispute_id"])

    def test_errors_carry_the_status_and_say_when_the_witness_is_unreachable(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            embedded = EmbeddedWitness(d, dev=True)
            self.addCleanup(embedded.stop)
            with self.assertRaises(WitnessError) as caught:
                embedded.client.record("nope", action())
            self.assertEqual((caught.exception.status, caught.exception.unreachable), (404, False))
            wrong = WitnessClient(embedded.client.base_url, api_key="wrong")
            with self.assertRaises(WitnessError) as caught:
                wrong.chain("m-w")                      # /healthz is open on purpose, so use a call that needs the key
            self.assertEqual(caught.exception.status, 401)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(WitnessError) as caught:
            WitnessClient(f"http://127.0.0.1:{port}", timeout=1).health()
        self.assertTrue(caught.exception.unreachable)

    def test_refuses_a_url_that_is_not_http(self):
        with self.assertRaises(ValueError):
            WitnessClient("witness.example")

    def test_works_with_a_remote_witness_behind_a_path(self):
        server = make_server(port=0, api_key="k", dev=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        client = WitnessClient(f"http://127.0.0.1:{server.server_address[1]}/", api_key="k")
        self.assertFalse(client.health()["persistent"])


class OpenMandateTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def test_a_dev_mandate_and_what_is_already_spent(self):
        embedded = EmbeddedWitness(os.path.join(self.dir, "w"), dev=True)
        self.addCleanup(embedded.stop)
        info = open_mandate(embedded.client, dev=DEV)
        self.assertEqual((info.mandate.mandate_id, info.spent, info.receipts), ("m-w", 0.0, 0))
        embedded.client.record("m-w", action(amount=30))
        embedded.client.record("m-w", action(amount=12.5))
        again = open_mandate(embedded.client, mandate_id="m-w")
        self.assertEqual((again.spent, again.receipts, again.witness_pub), (42.5, 2, embedded.witness_pub))

    def test_a_signed_mandate_file_is_registered_and_the_pinned_key_is_checked(self):
        user = Signer()
        signed, _ = quick_mandate(intent="buy shoes", merchants=["stridemart"], mandate_id="m-file", principal=user)
        path = os.path.join(self.dir, "mandate.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(signed_mandate_to_dict(signed), f)
        embedded = EmbeddedWitness(os.path.join(self.dir, "w"), principal_keys=[user.public_hex])
        self.addCleanup(embedded.stop)
        info = open_mandate(embedded.client, file=path, principal_key=user.public_hex)
        self.assertEqual(info.mandate.mandate_id, "m-file")
        self.assertEqual(open_mandate(embedded.client, file=path).receipts, 0)                    # registering twice is fine
        with self.assertRaisesRegex(WitnessError, "different key"):
            open_mandate(embedded.client, file=path, principal_key="00" * 32)

    def test_a_witness_that_only_trusts_one_user_refuses_another(self):
        trusted, stranger = Signer(), Signer()
        signed, _ = quick_mandate(intent="buy shoes", mandate_id="m-x", principal=stranger)
        path = os.path.join(self.dir, "mandate.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(signed_mandate_to_dict(signed), f)
        embedded = EmbeddedWitness(os.path.join(self.dir, "w"), principal_keys=[trusted.public_hex])
        self.addCleanup(embedded.stop)
        with self.assertRaises(WitnessError) as caught:
            open_mandate(embedded.client, file=path)
        self.assertEqual(caught.exception.status, 403)

    def test_problems_are_explained(self):
        embedded = EmbeddedWitness(os.path.join(self.dir, "w"), dev=True)
        self.addCleanup(embedded.stop)
        with self.assertRaisesRegex(WitnessError, "no mandate"):
            open_mandate(embedded.client)
        with self.assertRaisesRegex(WitnessError, "cannot read"):
            open_mandate(embedded.client, file=os.path.join(self.dir, "missing.json"))
        bad = os.path.join(self.dir, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write('{"mandate": 1}')
        with self.assertRaisesRegex(WitnessError, "not a valid signed mandate"):
            open_mandate(embedded.client, file=bad)
        with self.assertRaises(WitnessError) as caught:
            open_mandate(embedded.client, mandate_id="unknown")
        self.assertEqual(caught.exception.status, 404)

    def test_a_folder_that_cannot_be_used_is_reported(self):
        blocker = os.path.join(self.dir, "afile")
        with open(blocker, "w") as f:
            f.write("x")
        with self.assertRaisesRegex(WitnessError, "cannot use the data folder"):
            EmbeddedWitness(os.path.join(blocker, "inside"))

    def test_two_proxies_cannot_use_the_same_folder_at_once(self):
        folder = os.path.join(self.dir, "w")
        first = EmbeddedWitness(folder, dev=True)
        with self.assertRaisesRegex(WitnessError, "already using"):
            EmbeddedWitness(folder, dev=True)
        first.stop()
        EmbeddedWitness(folder, dev=True).stop()                                      # free again once the first has stopped

    def test_the_same_key_and_chain_carry_on_across_embedded_runs(self):
        folder = os.path.join(self.dir, "w")
        first = EmbeddedWitness(folder, dev=True)
        open_mandate(first.client, dev=DEV)
        first.client.record("m-w", action())
        key = first.witness_pub
        first.stop()
        second = EmbeddedWitness(folder, dev=True)
        self.addCleanup(second.stop)
        self.assertEqual(second.witness_pub, key)
        self.assertEqual(open_mandate(second.client, mandate_id="m-w").receipts, 1)


if __name__ == "__main__":
    unittest.main()
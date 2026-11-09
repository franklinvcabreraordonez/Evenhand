import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from evenhand import quick_mandate, verify_bundle
from evenhand.checkpoint import verify_bundle_with_checkpoint
from evenhand.serialize import signed_mandate_to_dict
from evenhand.server import make_server
from evenhand.store import LogCorrupt, Store, StoreError

INTENT = {"intent": "buy running shoes under $120", "merchants": "stridemart", "max_per_txn": 120, "categories": ["sports"]}


def _call(base, method, path, body=None, token=None, raw=None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        finally:
            e.close()


def receipt_body(mandate_id, amount=30, merchant="stridemart"):
    return {"mandate_id": mandate_id, "action": {"merchant": merchant, "amount": amount, "description": "road running shoes", "category": "sports"}}


class Folder(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)  # registered first, so it runs after every server has been stopped
        self.dir = tmp.name
        self.log = Path(self.dir) / "log.jsonl"
        self.keyfile = Path(self.dir) / "witness.key"

    def start(self, **kwargs):
        server = make_server(port=0, data_dir=self.dir, **kwargs)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def stop():
            server.shutdown()
            server.server_close()

        self.addCleanup(stop)
        return server, f"http://127.0.0.1:{server.server_address[1]}", stop

    def lines(self):
        return self.log.read_text(encoding="ascii").splitlines()

    def write_lines(self, lines):
        self.log.write_text("\n".join(lines) + "\n", encoding="ascii")

    def saved_records(self):
        store = Store(self.dir)
        try:
            return store.open_log()
        finally:
            store.close()

    def seed(self, receipts=3):
        """A witness with one mandate, some receipts and a checkpoint, stopped. Returns the checkpoint."""
        _, base, stop = self.start(dev=True)
        self.assertEqual(_call(base, "POST", "/v1/dev/mandates", dict(INTENT, mandate_id="m-1"))[0], 201)
        for i in range(receipts):
            self.assertEqual(_call(base, "POST", "/v1/receipts", receipt_body("m-1", 30 + i))[0], 201)
        status, cp = _call(base, "POST", "/v1/checkpoints", {"mandate_id": "m-1"})
        self.assertEqual(status, 201)
        stop()
        return cp


class StoreTests(Folder):
    def test_the_key_is_created_once_and_reused(self):
        first = Store(self.dir).signer().public_hex
        self.assertEqual(Store(self.dir).signer().public_hex, first)
        self.assertEqual(len(self.keyfile.read_text().strip()), 64)

    def test_a_damaged_key_is_refused_and_left_alone(self):
        self.keyfile.write_text("not a key\n")
        with self.assertRaises(StoreError):
            Store(self.dir).signer()
        self.assertEqual(self.keyfile.read_text(), "not a key\n")

    def test_records_round_trip_and_a_half_written_last_line_is_dropped(self):
        store = Store(self.dir)
        self.assertEqual(store.open_log(), [])
        store.append({"kind": "a", "n": 1})
        store.append({"kind": "a", "n": 2})
        store.close()
        good = self.log.stat().st_size
        with open(self.log, "ab") as f:
            f.write(b'{"kind": "a", "n"')               # the power went out mid-write
        again = Store(self.dir)
        self.assertEqual([r for _, r in again.open_log()], [{"kind": "a", "n": 1}, {"kind": "a", "n": 2}])
        self.assertEqual(self.log.stat().st_size, good)  # the stump is gone
        again.append({"kind": "a", "n": 3})
        again.close()
        self.assertEqual(len(self.saved_records()), 3)

    def test_a_complete_last_line_without_its_newline_is_kept(self):
        self.log.write_bytes(b'{"kind":"a","n":1}\n{"kind":"a","n":2}')
        store = Store(self.dir)
        self.assertEqual(len(store.open_log()), 2)
        store.append({"kind": "a", "n": 3})
        store.close()
        self.assertEqual(len(self.lines()), 3)

    def test_a_bad_line_in_the_middle_is_corruption(self):
        self.log.write_bytes(b'{"kind":"a"}\nnot json\n{"kind":"a"}\n')
        with self.assertRaisesRegex(LogCorrupt, "line 2"):
            self.saved_records()

    def test_a_second_writer_is_noticed(self):
        store = Store(self.dir)
        store.open_log()
        store.append({"kind": "a"})
        with open(self.log, "ab") as other:
            other.write(b'{"kind":"b"}\n')
        with self.assertRaisesRegex(StoreError, "changed by something else"):
            store.append({"kind": "a"})
        store.close()

    def test_a_failed_write_leaves_the_log_as_it_was(self):
        store = Store(self.dir)
        store.open_log()
        store.append({"kind": "a"})
        size, real = self.log.stat().st_size, store._fh

        class Full:
            def __getattr__(self, name):
                return getattr(real, name)

            def write(self, data):
                real.write(data[:5])
                raise OSError(28, "No space left on device")

        store._fh = Full()
        with self.assertRaisesRegex(StoreError, "could not save"):
            store.append({"kind": "a", "n": 2})
        store._fh = real
        self.assertEqual(self.log.stat().st_size, size)
        store.append({"kind": "a", "n": 3})
        store.close()
        self.assertEqual(len(self.saved_records()), 2)


class RestartTests(Folder):
    def test_everything_survives_a_restart_and_the_chain_carries_on(self):
        _, base, stop = self.start(dev=True)
        key = _call(base, "GET", "/healthz")[1]["witness_pub"]
        made = _call(base, "POST", "/v1/dev/mandates", dict(INTENT, mandate_id="m-1"))[1]
        for amount in (30, 40, 50):
            _call(base, "POST", "/v1/receipts", receipt_body("m-1", amount))
        cp = _call(base, "POST", "/v1/checkpoints", {"mandate_id": "m-1"})[1]
        dispute = _call(base, "POST", "/v1/disputes", {"mandate_id": "m-1", "seq": 1})[1]
        stop()

        _, base, stop = self.start(dev=True)
        health = _call(base, "GET", "/healthz")[1]
        self.assertEqual((health["witness_pub"], health["persistent"]), (key, True))
        bundle = _call(base, "GET", "/v1/chains/m-1")[1]
        self.assertTrue(verify_bundle(bundle, expected_principal_pub=made["principal_pub"], expected_witness_pub=key)["ok"])
        self.assertEqual(len(bundle["receipts"]), 3)
        self.assertEqual(_call(base, "GET", f"/v1/disputes/{dispute['dispute_id']}")[1]["verdict"]["type"], "within_mandate")
        self.assertEqual(_call(base, "GET", f"/v1/disputes/{dispute['dispute_id']}/packet")[1]["verdict"], "within_mandate")
        self.assertEqual(_call(base, "GET", "/v1/checkpoints/m-1")[1], cp)
        self.assertEqual(_call(base, "POST", "/v1/receipts", receipt_body("m-1", 60))[1]["seq"], 3)   # the chain carries on
        self.assertEqual(_call(base, "POST", "/v1/disputes", {"mandate_id": "m-1"})[1]["dispute_id"], "d-0002")
        out = verify_bundle_with_checkpoint(_call(base, "GET", "/v1/chains/m-1")[1], cp, expected_witness_pub=key,
                                            expected_principal_pub=made["principal_pub"])
        self.assertEqual((out["ok"], out["receipts_after_checkpoint"]), (True, 1))
        self.assertEqual(_call(base, "POST", "/v1/dev/revocations", {"mandate_id": "m-1"})[0], 404)   # throwaway keys are not saved
        self.assertEqual(_call(base, "POST", "/v1/dev/mandates", INTENT)[1]["mandate_id"], "m-dev-1")
        stop()

    def test_dev_mandate_names_do_not_clash_with_saved_ones(self):
        _, base, stop = self.start(dev=True)
        self.assertEqual(_call(base, "POST", "/v1/dev/mandates", INTENT)[1]["mandate_id"], "m-dev-1")
        stop()
        _, base, stop = self.start(dev=True)
        self.assertEqual(_call(base, "POST", "/v1/dev/mandates", INTENT)[1]["mandate_id"], "m-dev-2")

    def test_many_agents_at_once_survive_a_restart(self):
        _, base, stop = self.start(dev=True)
        _call(base, "POST", "/v1/dev/mandates", dict(INTENT, mandate_id="m-load"))

        def agent(n):
            for i in range(10):
                self.assertEqual(_call(base, "POST", "/v1/receipts", receipt_body("m-load", 10 + i))[0], 201)

        threads = [threading.Thread(target=agent, args=(n,)) for n in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        stop()
        _, base, stop = self.start(dev=True)
        out = _call(base, "GET", "/v1/verify?mandate_id=m-load")[1]
        self.assertEqual((out["ok"], out["receipts"]), (True, 80))


class TamperTests(Folder):
    def refuses_to_start(self, pattern):
        with self.assertRaisesRegex(LogCorrupt, pattern):
            make_server(port=0, data_dir=self.dir, dev=True)

    def test_an_edited_receipt_stops_the_witness_from_starting(self):
        self.seed()
        lines = self.lines()
        i = next(n for n, l in enumerate(lines) if '"kind":"receipt"' in l)
        rec = json.loads(lines[i])
        rec["receipt"]["action"]["amount"] = 1.0
        lines[i] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        self.write_lines(lines)
        self.refuses_to_start("do not verify")

    def test_a_deleted_middle_receipt_stops_the_witness_from_starting(self):
        self.seed()
        lines = self.lines()
        receipts = [n for n, l in enumerate(lines) if '"kind":"receipt"' in l]
        del lines[receipts[1]]
        self.write_lines(lines)
        self.refuses_to_start("do not verify")

    def test_an_edited_mandate_stops_the_witness_from_starting(self):
        self.seed()
        lines = self.lines()
        i = next(n for n, l in enumerate(lines) if '"kind":"mandate"' in l)
        rec = json.loads(lines[i])
        rec["mandate"]["mandate"]["constraints"]["max_per_txn"] = 100000.0
        lines[i] = json.dumps(rec, sort_keys=True, separators=(",", ":"))
        self.write_lines(lines)
        self.refuses_to_start("signature does not verify")

    def test_the_wrong_key_stops_the_witness_from_starting(self):
        self.seed()
        self.keyfile.write_text(os.urandom(32).hex() + "\n")
        self.refuses_to_start("different witness.key")

    def test_receipts_lost_from_before_a_saved_checkpoint_are_noticed(self):
        self.seed()
        lines = self.lines()
        last = max(n for n, l in enumerate(lines) if '"kind":"receipt"' in l)
        del lines[last]                                   # the checkpoint line is still there
        self.write_lines(lines)
        self.refuses_to_start("lost receipts")

    def test_a_failed_start_does_not_leave_the_folder_locked(self):
        self.seed()
        self.keyfile.write_text(os.urandom(32).hex() + "\n")
        self.refuses_to_start("different witness.key")   # the temporary folder can still be removed afterwards

    def test_truncation_the_server_cannot_see_is_caught_by_a_client_holding_a_checkpoint(self):
        cp = self.seed()
        lines = self.lines()
        keep = [l for l in lines if '"kind":"checkpoint"' not in l][:-1]   # drop the last receipt AND the checkpoint line
        self.write_lines(keep)
        _, base, _ = self.start(dev=True)                                   # the server alone cannot tell
        bundle = _call(base, "GET", "/v1/chains/m-1")[1]
        key = _call(base, "GET", "/healthz")[1]["witness_pub"]
        self.assertEqual(len(bundle["receipts"]), 2)
        self.assertTrue(verify_bundle(bundle, expected_witness_pub=key)["ok"])           # it verifies on its own
        out = verify_bundle_with_checkpoint(bundle, cp, expected_witness_pub=key)         # but the saved checkpoint exposes it
        self.assertFalse(out["ok"])
        self.assertTrue(any("cut off" in p for p in out["problems"]))


class EndpointTests(Folder):
    def test_checkpoint_endpoints(self):
        _, base, _ = self.start(dev=True)
        _call(base, "POST", "/v1/dev/mandates", dict(INTENT, mandate_id="m-1"))
        self.assertEqual(_call(base, "POST", "/v1/checkpoints", {"mandate_id": "m-1"})[0], 409)       # nothing recorded yet
        self.assertEqual(_call(base, "GET", "/v1/checkpoints/m-1")[0], 404)                            # none issued yet
        self.assertEqual(_call(base, "POST", "/v1/checkpoints", {"mandate_id": "nope"})[0], 404)
        self.assertEqual(_call(base, "GET", "/v1/checkpoints/nope")[0], 404)
        self.assertEqual(_call(base, "POST", "/v1/checkpoints", {})[0], 400)
        _call(base, "POST", "/v1/receipts", receipt_body("m-1"))
        status, first = _call(base, "POST", "/v1/checkpoints", {"mandate_id": "m-1"})
        self.assertEqual((status, first["count"]), (201, 1))
        status, again = _call(base, "POST", "/v1/checkpoints", {"mandate_id": "m-1"})
        self.assertEqual((status, again), (200, first))                                                # nothing new, same checkpoint
        _call(base, "POST", "/v1/receipts", receipt_body("m-1"))
        status, second = _call(base, "POST", "/v1/checkpoints", {"mandate_id": "m-1"})
        self.assertEqual((status, second["count"]), (201, 2))
        self.assertEqual(_call(base, "GET", "/v1/checkpoints/m-1")[1], second)
        self.assertEqual(sum('"kind":"checkpoint"' in l for l in self.lines()), 2)                    # saved once each

    def test_a_server_without_a_folder_still_issues_checkpoints(self):
        server = make_server(port=0, dev=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        base = f"http://127.0.0.1:{server.server_address[1]}"
        self.assertFalse(_call(base, "GET", "/healthz")[1]["persistent"])
        _call(base, "POST", "/v1/dev/mandates", dict(INTENT, mandate_id="m-1"))
        _call(base, "POST", "/v1/receipts", receipt_body("m-1"))
        self.assertEqual(_call(base, "POST", "/v1/checkpoints", {"mandate_id": "m-1"})[0], 201)

    def test_the_bearer_token_protects_checkpoints(self):
        _, base, _ = self.start(dev=True, api_key="s3cret")
        self.assertEqual(_call(base, "POST", "/v1/checkpoints", {"mandate_id": "m-1"})[0], 401)
        self.assertEqual(_call(base, "GET", "/v1/checkpoints/m-1")[0], 401)

    def test_when_saving_fails_nothing_is_recorded(self):
        server, base, _ = self.start(dev=True)
        _call(base, "POST", "/v1/dev/mandates", dict(INTENT, mandate_id="m-1"))
        _call(base, "POST", "/v1/receipts", receipt_body("m-1", 30))
        _call(base, "POST", "/v1/checkpoints", {"mandate_id": "m-1"})
        real = server.state.store.append

        def full_disk(record):
            raise StoreError("disk full")

        server.state.store.append = full_disk
        signed, _ = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"], mandate_id="m-2")
        self.assertEqual(_call(base, "POST", "/v1/receipts", receipt_body("m-1", 31))[0], 503)
        self.assertEqual(_call(base, "POST", "/v1/mandates", signed_mandate_to_dict(signed))[0], 503)
        self.assertEqual(_call(base, "POST", "/v1/dev/mandates", dict(INTENT, mandate_id="m-3"))[0], 503)
        self.assertEqual(_call(base, "POST", "/v1/disputes", {"mandate_id": "m-1"})[0], 503)
        self.assertEqual((_call(base, "GET", "/v1/chains/m-2")[0], _call(base, "GET", "/v1/chains/m-3")[0]), (404, 404))   # never registered
        self.assertEqual(_call(base, "GET", "/v1/verify?mandate_id=m-1")[1]["receipts"], 1)            # still one receipt, no gap
        server.state.store.append = real
        self.assertEqual(_call(base, "POST", "/v1/receipts", receipt_body("m-1", 33))[1]["seq"], 1)    # the chain carries on from 1
        server.state.store.append = full_disk
        self.assertEqual(_call(base, "POST", "/v1/checkpoints", {"mandate_id": "m-1"})[0], 503)
        self.assertEqual(_call(base, "GET", "/v1/checkpoints/m-1")[1]["count"], 1)                      # the old checkpoint is still the latest
        server.state.store.append = real
        self.assertTrue(_call(base, "GET", "/v1/verify?mandate_id=m-1")[1]["ok"])
        self.assertEqual(_call(base, "POST", "/v1/checkpoints", {"mandate_id": "m-1"})[1]["count"], 2)


class CommandLineTests(Folder):
    def run_server(self):
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.Popen([sys.executable, "-u", "-m", "evenhand.server", "--port", "0", "--data-dir", self.dir, "--dev"],
                                cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close()))
        seen = []
        port = key = None
        while key is None or not any(l.startswith(("Saving to", "Nothing is saved")) for l in seen):
            line = proc.stdout.readline()
            self.assertTrue(line, "the server exited early:\n" + "".join(seen))
            seen.append(line)
            if line.startswith("Evenhand witness on"):
                port = int(line.strip().rsplit(":", 1)[1])
            if line.startswith("Witness public key"):
                key = line.strip().rsplit(" ", 1)[1]
        return proc, f"http://127.0.0.1:{port}", key, seen

    def test_the_key_and_the_records_survive_a_real_restart(self):
        proc, base, key, seen = self.run_server()
        self.assertTrue(any("Nothing is saved" not in l and "Saving to" in l for l in seen))
        self.assertEqual(_call(base, "GET", "/healthz")[1]["witness_pub"], key)
        _call(base, "POST", "/v1/dev/mandates", dict(INTENT, mandate_id="m-1"))
        _call(base, "POST", "/v1/receipts", receipt_body("m-1"))
        proc.kill()
        proc.wait()
        proc.stdout.close()
        proc2, base2, key2, seen2 = self.run_server()
        self.assertEqual(key2, key)
        self.assertTrue(any("1 mandates, 1 receipts" in l for l in seen2), seen2)
        self.assertEqual(_call(base2, "GET", "/v1/verify?mandate_id=m-1")[1]["receipts"], 1)


if __name__ == "__main__":
    unittest.main()
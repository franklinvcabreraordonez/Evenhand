"""A witness that remembers, and a checkpoint that catches cut-off receipts.

Run from the repository root:  python examples/checkpoint_demo.py
It uses a temporary folder, so nothing is left behind.
"""
import json
import sys
import tempfile
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # works without installing

from evenhand import verify_bundle
from evenhand.checkpoint import verify_bundle_with_checkpoint
from evenhand.server import make_server

folder = tempfile.TemporaryDirectory()
log = Path(folder.name) / "log.jsonl"


def start():
    server = make_server(port=0, data_dir=folder.name, dev=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def stop(server):
    server.shutdown()
    server.server_close()


def call(base, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def buy(base, n):
    for i in range(n):
        call(base, "POST", "/v1/receipts", {"mandate_id": "m-1", "action": {
            "merchant": "stridemart", "amount": 20 + i, "description": "road running shoes", "category": "sports"}})


server, base = start()
witness_key = call(base, "GET", "/healthz")["witness_pub"]
made = call(base, "POST", "/v1/dev/mandates", {"intent": "buy running shoes under $120", "merchants": "stridemart",
                                              "max_per_txn": 120, "categories": ["sports"], "mandate_id": "m-1"})
user_key = made["principal_pub"]
buy(base, 3)
checkpoint = call(base, "POST", "/v1/checkpoints", {"mandate_id": "m-1"})
print(f"1. three purchases witnessed. Checkpoint saved by the client: {checkpoint['count']} receipts, head {checkpoint['head'][:12]}...")
buy(base, 2)
print("2. two more purchases witnessed after the checkpoint.")
stop(server)

server, base = start()
print("3. the witness was stopped and started again. Same key:", call(base, "GET", "/healthz")["witness_pub"] == witness_key)
bundle = call(base, "GET", "/v1/chains/m-1")
out = verify_bundle_with_checkpoint(bundle, checkpoint, expected_witness_pub=witness_key, expected_principal_pub=user_key)
print(f"4. {len(bundle['receipts'])} receipts survived. Checked against the saved checkpoint: ok={out['ok']}, "
      f"{out['receipts_after_checkpoint']} receipts added since.")
stop(server)

# someone with access to the folder deletes the newest receipts and the checkpoint record, hoping nobody notices
kept = [l for l in log.read_text(encoding="ascii").splitlines() if '"kind":"checkpoint"' not in l]
receipt_lines = [i for i, l in enumerate(kept) if '"kind":"receipt"' in l]
log.write_text("\n".join(kept[:receipt_lines[1] + 1]) + "\n", encoding="ascii")   # back to two receipts, fewer than the checkpoint covers
server, base = start()
cut = call(base, "GET", "/v1/chains/m-1")
print(f"5. the log was cut back to {len(cut['receipts'])} receipts. The witness starts, and the chain verifies by itself: "
      f"{verify_bundle(cut, expected_witness_pub=witness_key, expected_principal_pub=user_key)['ok']}")
out = verify_bundle_with_checkpoint(cut, checkpoint, expected_witness_pub=witness_key, expected_principal_pub=user_key)
print(f"6. checked against the checkpoint saved earlier: ok={out['ok']}")
print("   ", out["problems"][0])
stop(server)
folder.cleanup()
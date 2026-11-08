"""The same flow over HTTP, for an agent written in any language.

Run from the repository root:  python examples/http_demo.py
It starts a witness on a free local port, then plays the part of an agent and a merchant's risk team.
To run the server on its own:   python -m evenhand.server --port 8787
"""
import json
import sys
import threading
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # works without installing

from evenhand import quick_mandate, verify_bundle
from evenhand.serialize import signed_mandate_to_dict
from evenhand.server import make_server

server = make_server(port=0)
threading.Thread(target=server.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{server.server_address[1]}"


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


signed, principal = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"],
                                  max_per_txn=120, categories=["sports"], mandate_id="m-demo")
print("1. register the signed mandate ->", call("POST", "/v1/mandates", signed_mandate_to_dict(signed))["digest"][:16], "...")

for merchant, amount, item in (("stridemart", 99, "road running shoes"), ("shadymart", 89, "running shoes")):
    r = call("POST", "/v1/receipts", {"mandate_id": "m-demo", "action": {
        "merchant": merchant, "amount": amount, "description": item, "category": "sports"}})
    print(f"2. witnessed receipt #{r['seq']}: {merchant} ${amount}  (hash {r['hash'][:12]}...)")

for seq in (0, 1):
    d = call("POST", "/v1/disputes", {"mandate_id": "m-demo", "seq": seq})
    print(f"3. dispute on #{seq}: {d['verdict']['type']} ({d['status']})  {d['verdict']['reasons']}")

packet = call("GET", f"/v1/disputes/{d['dispute_id']}/packet")
print("4. evidence packet:", packet["verdict"], "|", packet["recommendation"])

bundle = call("GET", "/v1/chains/m-demo")
witness_pub = call("GET", "/healthz")["witness_pub"]  # in real use, get this key from a channel you trust, not from the same server
print("5. offline check of the exported chain, with both keys pinned:",
      verify_bundle(bundle, expected_principal_pub=principal.public_hex, expected_witness_pub=witness_pub)["ok"])
server.shutdown()
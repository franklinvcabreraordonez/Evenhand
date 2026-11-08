"""Witness an agent's purchases in a few lines, then settle a dispute with evidence.

Run from the repository root:  python examples/witness_in_30_minutes.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # works without installing

from evenhand import Gateway, quick_mandate, verify_bundle

# 1. The user's wallet signs what the agent may do. (quick_mandate does it for you in development.)
signed, principal = quick_mandate(
    intent="buy running shoes under $120", agent_id="shopping-agent-1",
    merchants=["stridemart"], max_per_txn=120, categories=["sports"],
)

# 2. Put a witness on the agent's tool path. Pin the user's key so a swapped mandate is caught.
gw = Gateway(signed, expected_principal_pub=principal.public_hex)


@gw.tool(category="sports")  # <- the one line that matters
def buy(merchant, amount, description):
    """Your agent's real purchase call goes here."""
    return f"ordered {description} from {merchant} for ${amount:.2f}"


# 3. The agent works as before. Each successful call is signed and chained.
print(buy("stridemart", 99.0, "road running shoes"))
print(buy("shadymart", 89.0, "running shoes"))

# 4. A dispute arrives. Ask for a verdict on any witnessed action.
for seq in (0, 1):
    d = gw.dispute(seq)
    print(f"purchase {seq}: {d.verdict.type.value}  {d.verdict.reasons}")

# 5. Hand a third party everything they need to check the record themselves, offline.
bundle = json.loads(json.dumps(gw.export()))
print("bundle verifies:", verify_bundle(bundle, expected_principal_pub=principal.public_hex)["ok"])
bundle["receipts"][1]["action"]["merchant_id"] = "stridemart"  # someone rewrites history
print("after an edit:  ", verify_bundle(bundle)["problems"][0])
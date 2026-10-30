"""Evenhand quickstart: witness an agent's purchases, then settle a dispute with evidence.

Run from the repository root:  python examples/quickstart.py
"""
import dataclasses
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # works without installing

from evenhand.adjudicator import adjudicate
from evenhand.crypto import Signer
from evenhand.evidence import build_packet, render_markdown
from evenhand.mandate import Constraints, Mandate, sign_mandate
from evenhand.receipts import AgentAction, CartItem, Witness

now = time.time()
principal = Signer()  # the user, or their wallet
witness = Witness()   # runs outside the agent's control

# 1. The user signs a scoped, expiring mandate for one agent.
mandate = Mandate(
    mandate_id="m-001", principal_id="user-42", agent_id="shopping-agent-1",
    issued_at=now - 60, expires_at=now + 86_400, actions=("purchase",),
    constraints=Constraints(max_per_txn=120.0, max_total=300.0, merchants_allow=("stridemart",), categories_allow=("sports",)),
    intent="buy running shoes under $120",
)
signed = sign_mandate(mandate, principal)


def purchase(merchant, desc, amount):
    action = AgentAction(mandate.mandate_id, mandate.agent_id, "purchase", amount, "USD", merchant, "sports",
                         (CartItem("sku-1", desc, amount),), True, time.time())
    return witness.record(action)


# 2. The agent acts; the witness signs what it saw.
ok = purchase("stridemart", "road running shoes", 99.0)   # inside the mandate
bad = purchase("shadymart", "running shoes", 89.0)        # merchant not allowed
chain = witness.chain(mandate.mandate_id)


def judge(label, receipts, seq):
    verdict = adjudicate(signed, receipts, seq, witness.public_hex, expected_principal_pub=principal.public_hex)
    print(f"{label}: {verdict.type.value}")
    for reason in verdict.reasons:
        print("   -", reason)
    return verdict


# 3. A dispute arrives. Each purchase gets a verdict.
judge("Purchase 1 (stridemart, $99)", chain, ok.seq)
verdict = judge("Purchase 2 (shadymart, $89)", chain, bad.seq)

# 4. Tampering is detected: edit a signed receipt and the evidence is rejected.
forged = list(chain)
forged[1] = dataclasses.replace(chain[1], action=dataclasses.replace(chain[1].action, merchant_id="stridemart"))
judge("Purchase 2 with an edited receipt", forged, 1)

# 5. The packet an issuer or merchant would receive.
packet = build_packet("case-001", signed, chain, bad.seq, verdict, witness.public_hex)
print()
print(render_markdown(packet))

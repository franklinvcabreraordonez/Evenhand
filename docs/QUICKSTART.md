# Witness your agent in 30 minutes

Evenhand records what an AI agent did from outside the agent's control, checks it against what the user signed, and returns a verdict plus an evidence packet. This page takes you from nothing to a witnessed agent.

> Status: a v0.2 prototype. Synthetic data only, not audited, not for real money or customer data.

## Wrap the function your agent spends with (5 minutes)

```python
from evenhand import Gateway, quick_mandate, verify_bundle

# The user's wallet signs what the agent may do. quick_mandate stands in for it in development.
signed, principal = quick_mandate(intent="buy running shoes under $120", merchants=["stridemart"],
                                  max_per_txn=120, categories=["sports"])
gw = Gateway(signed, expected_principal_pub=principal.public_hex)

@gw.tool(category="sports")           # the one line that matters
def buy(merchant, amount, description):
    ...                               # your real purchase call goes here

buy("stridemart", 99.0, "road running shoes")
buy("shadymart", 89.0, "running shoes")

print(gw.dispute(0).verdict.type.value)   # within_mandate
print(gw.dispute(1).verdict.type.value)   # exceeded_mandate
bundle = gw.export()                      # plain JSON; anyone can verify it offline
print(verify_bundle(bundle, expected_principal_pub=principal.public_hex)["ok"])
```

Run `python examples/witness_in_30_minutes.py` to see this work, including what happens when someone edits a record after the fact.

- A successful call is signed and chained to the one before it. A call that raises is not recorded.
- By default the call's `merchant`, `amount` and `description` (or `item`, `desc`, `product`) arguments are recorded. For any other signature pass `extract=lambda args: {...}`.
- Async functions and calls from several threads are supported.
- `gw.dispute(seq)` returns the verdict and the evidence packet. `verify_bundle` needs no server and no trust in whoever sent the bundle.

## Limits, stated plainly

- A witness running beside your code is a stand-in. The point of the design is a witness outside the agent's trust boundary, such as a gateway or proxy. That hosted witness does not exist yet.
- Intent matching is a keyword baseline and misjudges some valid purchases with messy product names.
- Verifiable Intent and AP2 adapters are not built.
- Receipts live in memory and are lost when the process stops, unless you export them.'

## Witness over HTTP (any language)

Run `python -m evenhand.server --dev`, then send each purchase your agent makes to `POST /v1/receipts`. A walkthrough with curl, Node.js and PowerShell is in [HTTP-API.md](HTTP-API.md). The development helpers sign mandates with throwaway keys. For anything real, start the server with `--principal-key` and the user's public key.
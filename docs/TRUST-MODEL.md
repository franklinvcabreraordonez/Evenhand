# Trust model: verifying an Evenhand record without trusting Evenhand

**The claim:** anyone can check a record offline with the open-source verifier. You do not have to trust Evenhand's servers, only two public keys that you pin from a source other than the record itself: the user's key and the witness's key.

## What is signed

| Object | Signed by | What the signature covers |
|---|---|---|
| Mandate (what the agent may do) | The user's key | The whole mandate: merchants, limits, expiry, intent |
| Receipt (what the agent did) | The witness's key | Its position in the chain, the hash of the previous receipt, and the action |
| Revocation | The user's key | The mandate id and the time of revocation |

Each receipt carries the hash of the one before it, so receipts form a chain. A bundle is the mandate, the receipts and the witness's public key as plain JSON.

## What a verifier checks

1. The mandate's signature verifies, and the signer is the user key you expected.
2. Every receipt's signature verifies under the witness key you expected.
3. Receipts are numbered 0, 1, 2 in order, and each links to the hash of the one before.
4. Every receipt belongs to this mandate.
5. Optionally, the last receipt matches a checkpoint (a head hash) the witness published.

## What this catches

| Attack | How it shows |
|---|---|
| Edit an amount or merchant in a receipt | Its signature no longer verifies |
| Delete or reorder a receipt | The numbering and hash links break |
| Swap in a mandate signed by someone else | The signer is not the expected user key |
| Replace the receipts with a chain from another witness | The witness key is not the one you pinned |
| Cut receipts off the end of the chain | Caught only if you hold a published checkpoint |

## What it does not protect against

- **A witness that records something false.** A signature proves a record was not changed afterwards. It does not prove the action happened as recorded. This is why the witness must sit outside the agent's trust boundary, and why a second witness (the merchant's own receipt) is planned.
- **Stolen keys.** Whoever holds the user's or the witness's private key can sign anything.
- **Time.** Timestamps are the witness's own clock. Nothing independent attests to them yet.
- **Unpinned verification.** Without pinned keys, a check proves only that a bundle is internally consistent. Someone can build a fresh, consistent chain under their own witness.

## Not built yet

A hosted witness outside the agent's process, published witness checkpoints in a public log, a second independent witness, key rotation, and adapters for Verifiable Intent and AP2. Today the witness is a library that runs beside your code.

## Check a record yourself

```python
from evenhand import verify_bundle

result = verify_bundle(
    bundle,
    expected_principal_pub=USER_PUBLIC_KEY,     # from the user's wallet, not from the bundle
    expected_witness_pub=WITNESS_PUBLIC_KEY,    # published by the witness, not from the bundle
    expected_head=PUBLISHED_CHECKPOINT,         # optional: catches truncation
)
print(result["ok"], result["problems"])
```
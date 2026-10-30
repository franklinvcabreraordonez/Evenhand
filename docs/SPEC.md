# Evenhand evidence format, v0.1 (draft)

This describes what the prototype in this repository implements. It is a working draft, not a standard.

## Encoding

- **Canonical JSON:** keys sorted, no whitespace, ASCII only. All signatures and hashes are computed over these bytes.
- **Hash:** SHA-256, hex encoded. **Signatures:** Ed25519, hex encoded. **Public keys:** raw 32-byte Ed25519, hex encoded.

## Mandate

The principal's signed, scoped, expiring authority for one agent. Fields: `mandate_id`, `principal_id`, `agent_id`, `issued_at`, `expires_at`, `actions`, `constraints`, `intent`.

Constraints: `max_per_txn`, `max_total`, `currency`, `merchants_allow` (null means any), `merchants_deny`, `categories_allow` (null means any), `step_up_above` (amounts above this need a human confirmation).

The signature is over the canonical JSON of the mandate. A **revocation** is a separately signed record over `{mandate_id, revoked_at, type: "revocation"}` by the same principal key.

## Receipt and chain

A witness records each agent action as a receipt. One chain per mandate.

- Receipt body: `{seq, prev_hash, action, witness_pub}`. The witness signs the canonical body.
- Receipt hash: SHA-256 of the canonical body bytes followed by the signature bytes.
- `prev_hash` of the first receipt is 64 zeros; each later receipt carries the previous receipt's hash.
- Action fields: `mandate_id`, `agent_id`, `action`, `amount`, `currency`, `merchant_id`, `category`, `cart` (items with `sku`, `desc`, `price`, `qty`), `step_up_done`, `ts`.

**Chain verification** checks, for every receipt: the witness key matches; the signature verifies; `seq` is contiguous from zero; the hash link matches; the receipt belongs to the mandate. Optionally the chain head is compared with a published checkpoint, which is the only way to detect truncation of the end of a chain.

## Adjudication

Checks run in this order, and every hard check is evaluated so the packet lists all violations:

1. Mandate signature verifies (and the signer is the expected principal key, if one is supplied). Failure gives `evidence_invalid`.
2. Chain verification passes. Failure gives `evidence_invalid`.
3. The disputed receipt is present. Otherwise `evidence_insufficient`.
4. Hard constraints: agent binding; action type allowed; inside the validity window; not revoked before the action; per-transaction cap; cumulative cap across earlier receipts; merchant allow and deny lists; category; step-up confirmation. Any failure gives `exceeded_mandate`.
5. Intent: a matcher returns a score from 0 to 1. At or above 0.75 the verdict is `within_mandate`; at or below 0.25 it is `exceeded_mandate`; in between it is `ambiguous_intent` and goes to a human.

## Verdicts

`within_mandate`, `exceeded_mandate`, `ambiguous_intent`, `evidence_insufficient`, `evidence_invalid`.

## Evidence packet

Verdict, recommended action, reasons, intent score, every check with pass or fail and detail, the mandate digest and signer, the witness key and chain head, the disputed receipt, and the number of receipts supplied. Rendered as JSON or Markdown.

## Known gaps

- The mandate here is a simplified internal format. Adapters for Verifiable Intent and AP2 are planned and not built.
- The witness is a library. A hosted witness with published checkpoints does not exist yet.
- The intent matchers are heuristic baselines; the model-based matcher is a stub.
- Timestamps come from the witness and are not independently attested.
- No audit has been done. Do not use this for real money.

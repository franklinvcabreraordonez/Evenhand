# Witness over HTTP (any language)

An agent written in any language can be witnessed by sending each action to a small HTTP server. The server records it, signs it, chains it to the one before, and later returns a verdict, an evidence packet, an exported chain that anyone can check offline, or a signed checkpoint that proves how long the chain was.

> Status: a prototype for testing. There is no TLS and there is no rate limiting. Run it on your own machine and do not expose it to the internet. Unless you start it with `--data-dir`, it remembers nothing after it stops.

## Start it

```
python -m evenhand.server --dev --data-dir witness-data
```

`--dev` adds helpers that sign mandates for you with throwaway keys, so you can try the flow with plain `curl`. For anything real, give the server the user's public key instead, and it will refuse mandates signed by anyone else:

```
python -m evenhand.server --principal-key <USER_PUBLIC_KEY> --data-dir witness-data
```

It prints the witness's public key when it starts. Publish that key, so others can pin it.

| Flag | What it does |
|---|---|
| `--port 8787` | Port to listen on (default 8787) |
| `--host 127.0.0.1` | Address to bind. Keep the default unless you know why |
| `--data-dir FOLDER` | Keep the witness key and everything recorded in this folder, and carry on from it after a restart |
| `--api-key SECRET` | Require `Authorization: Bearer SECRET` on every request except `/healthz` |
| `--principal-key HEX` | Accept mandates only from this user key. Repeat the flag for several keys |
| `--dev` | Enable `/v1/dev/*`. Cannot be combined with `--principal-key` |

## Try it in five minutes (curl)

```
curl -s localhost:8787/healthz
curl -s -X POST localhost:8787/v1/dev/mandates -H "Content-Type: application/json" \
  -d '{"intent":"buy running shoes under $120","merchants":"stridemart","max_per_txn":120,"categories":["sports"],"mandate_id":"m-1"}'
curl -s -X POST localhost:8787/v1/receipts -H "Content-Type: application/json" \
  -d '{"mandate_id":"m-1","action":{"merchant":"shadymart","amount":89,"description":"running shoes","category":"sports"}}'
curl -s -X POST localhost:8787/v1/disputes -H "Content-Type: application/json" -d '{"mandate_id":"m-1"}'
curl -s localhost:8787/v1/disputes/d-0001/packet
curl -s -X POST localhost:8787/v1/checkpoints -H "Content-Type: application/json" -d '{"mandate_id":"m-1"}'
curl -s localhost:8787/v1/chains/m-1
```

The dispute comes back as `exceeded_mandate`, because `shadymart` is not on the user's list.

## Keeping what it records

With `--data-dir`, the folder holds two files: `witness.key`, the witness's private signing key, which you must keep secret and back up, and `log.jsonl`, one record per line, each saved to disk before the witness replies. After a restart the witness has the same key and carries on where it left off.

When it starts, it checks everything it saved: every mandate and receipt signature, every hash link, every checkpoint. If any of it fails, it refuses to start and says why, instead of quietly serving altered records. Run one witness per folder, and back up both files together.

If a save fails (a full disk, say), the request fails with a 503 and nothing is recorded, so the chain never has a gap.

What the witness cannot see for itself: someone who can edit the folder can delete the newest records without leaving a trace the witness can detect. That is what checkpoints are for.

## Checkpoints: catching receipts cut off the end

A checkpoint is the witness's signed statement: "for this mandate I had recorded N receipts, and the last one had this hash." Ask for one, and keep it somewhere the witness cannot reach.

Later, check any exported chain against it. The chain must still contain the checkpointed receipt, so cut-off or rewritten history fails, even though a shorter chain verifies on its own:

```python
from evenhand.checkpoint import verify_bundle_with_checkpoint

result = verify_bundle_with_checkpoint(
    bundle,                                   # from GET /v1/chains/{mandate_id}
    saved_checkpoint,                         # the checkpoint you saved earlier
    expected_witness_pub=WITNESS_PUBLIC_KEY,  # published by the witness, not from the bundle
    expected_principal_pub=USER_PUBLIC_KEY,   # from the user's wallet, not from the bundle
)
print(result["ok"], result["problems"])
```

A checkpoint protects what it covers. Receipts added after your newest checkpoint can still be cut until you take another, so ask for checkpoints regularly. Run `python examples/checkpoint_demo.py` to watch a cut-off get caught.

## From Node.js

```js
const base = 'http://127.0.0.1:8787';
const post = async (path, body) =>
  (await fetch(base + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();

await post('/v1/dev/mandates', { intent: 'buy running shoes under $120', merchants: ['stridemart'], max_per_txn: 120, categories: ['sports'], mandate_id: 'm-1' });
await post('/v1/receipts', { mandate_id: 'm-1', action: { merchant: 'shadymart', amount: 89, description: 'running shoes', category: 'sports' } });
const dispute = await post('/v1/disputes', { mandate_id: 'm-1' });
console.log(dispute.verdict.type);   // exceeded_mandate
const checkpoint = await post('/v1/checkpoints', { mandate_id: 'm-1' });
console.log(checkpoint.count);       // 1
```

## From Windows PowerShell

This uses only standard cmdlets. Use single quotes around text that contains a dollar sign.

```powershell
$base = 'http://127.0.0.1:8787'
$json = 'application/json'
$body = @{ intent = 'buy running shoes under $120'; merchants = 'stridemart'; max_per_txn = 120; categories = @('sports'); mandate_id = 'm-1' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$base/v1/dev/mandates" -ContentType $json -Body $body | Out-Null
$body = @{ mandate_id = 'm-1'; action = @{ merchant = 'shadymart'; amount = 89; description = 'running shoes'; category = 'sports' } } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "$base/v1/receipts" -ContentType $json -Body $body | Out-Null
(Invoke-RestMethod -Method Post -Uri "$base/v1/disputes" -ContentType $json -Body (@{ mandate_id = 'm-1' } | ConvertTo-Json)).verdict.type
(Invoke-RestMethod -Method Post -Uri "$base/v1/checkpoints" -ContentType $json -Body (@{ mandate_id = 'm-1' } | ConvertTo-Json)).count
```

## Endpoints

| Method and path | What it does | Success |
|---|---|---|
| `GET /healthz` | Status, the witness's public key, and whether dev mode, the allow-list and saving are on | 200 |
| `POST /v1/mandates` | Register a mandate signed by the user's key (the same call twice is fine) | 201, or 200 if already registered |
| `POST /v1/receipts` | Record one action. Body: `{"mandate_id": "...", "action": {...}}` | 201 |
| `POST /v1/disputes` | Judge one recorded action. Body: `{"mandate_id": "...", "seq": 1}`. Leave out `seq` for the latest. Add `"revocations": [...]` if the user revoked | 201 |
| `GET /v1/disputes/{id}` | The verdict and its status | 200 |
| `GET /v1/disputes/{id}/packet` | The evidence packet | 200 |
| `POST /v1/checkpoints` | Sign a checkpoint for a mandate. Body: `{"mandate_id": "..."}`. Asking again with nothing new returns the same one | 201, or 200 if nothing is new |
| `GET /v1/checkpoints/{mandate_id}` | The latest checkpoint issued for a mandate | 200 |
| `GET /v1/chains/{mandate_id}` | The exported bundle, for checking offline | 200 |
| `GET /v1/verify?mandate_id=...` | The witness checks its own chain | 200 |
| `POST /v1/dev/mandates` | Dev only: create, sign and register a mandate | 201 |
| `POST /v1/dev/revocations` | Dev only: sign a revocation for a mandate made that way in this run | 201 |

The action fields are `merchant` (or `merchant_id`), `amount`, and either `description` or `cart`. Also send `category` if the mandate limits categories, and `step_up_done` if the user confirmed a large purchase. The witness stamps the time itself and ignores any time the client sends.

## Errors

| Code | Meaning |
|---|---|
| 400 | Malformed input. The message names the field |
| 401 | Missing or wrong bearer token |
| 403 | The mandate's user key is not on the allow-list |
| 404 | Unknown mandate, dispute, checkpoint or path |
| 409 | A different mandate is already registered under that id, or there is nothing recorded to checkpoint yet |
| 413 | The request body is over 1 MB |
| 422 | The mandate's signature does not verify |
| 503 | The witness could not save to its folder, so nothing was recorded |

## What to trust

- **Pin both keys when you verify an exported chain.** Take the user's key from the user's wallet and the witness's key from the witness's operator, then pass both to `verify_bundle`. Without them a check proves only that the bundle is internally consistent. See [TRUST-MODEL.md](TRUST-MODEL.md).
- **`/healthz` reports the witness key for convenience.** A key you get from the server you are checking is not an independent source.
- **Without `--data-dir`, the witness key changes at every start,** so nobody can pin it, and everything recorded is lost.
- **Protect `witness.key`.** Whoever holds it can sign receipts and checkpoints as the witness. It is stored unencrypted.
- **A checkpoint does not stop a witness from showing different people different histories.** Catching that needs checkpoints published where everyone sees the same ones. That is not built yet.
- **Dev mode defeats the point of a user-held key,** because the server holds it. Use it for testing only.
- **Without `--principal-key,`** the first key to register a mandate is trusted as its user.
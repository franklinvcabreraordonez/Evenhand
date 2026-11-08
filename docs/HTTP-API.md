# Witness over HTTP (any language)

An agent written in any language can be witnessed by sending each action to a small HTTP server. The server records it, signs it, chains it to the one before, and later returns a verdict, an evidence packet, or an exported chain that anyone can check offline.

> Status: a prototype for testing. State lives in memory and is lost on restart, the witness key is new on every start, there is no TLS, and there is no rate limiting. Run it on your own machine and do not expose it to the internet.

## Start it

```
python -m evenhand.server --dev
```

`--dev` adds helpers that sign mandates for you with throwaway keys, so you can try the flow with plain `curl`. For anything real, give the server the user's public key instead, and it will refuse mandates signed by anyone else:

```
python -m evenhand.server --principal-key <USER_PUBLIC_KEY>
```

| Flag | What it does |
|---|---|
| `--port 8787` | Port to listen on (default 8787) |
| `--host 127.0.0.1` | Address to bind. Keep the default unless you know why |
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
curl -s localhost:8787/v1/chains/m-1
```

The dispute comes back as `exceeded_mandate`, because `shadymart` is not on the user's list.

## From Node.js

```js
const base = 'http://127.0.0.1:8787';
const post = async (path, body) =>
  (await fetch(base + path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })).json();

await post('/v1/dev/mandates', { intent: 'buy running shoes under $120', merchants: ['stridemart'], max_per_txn: 120, categories: ['sports'], mandate_id: 'm-1' });
await post('/v1/receipts', { mandate_id: 'm-1', action: { merchant: 'shadymart', amount: 89, description: 'running shoes', category: 'sports' } });
const dispute = await post('/v1/disputes', { mandate_id: 'm-1' });
console.log(dispute.verdict.type);   // exceeded_mandate
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
```

## Endpoints

| Method and path | What it does | Success |
|---|---|---|
| `GET /healthz` | Status, the witness's public key, and whether dev mode and the allow-list are on | 200 |
| `POST /v1/mandates` | Register a mandate signed by the user's key (the same call twice is fine) | 201, or 200 if already registered |
| `POST /v1/receipts` | Record one action. Body: `{"mandate_id": "...", "action": {...}}` | 201 |
| `POST /v1/disputes` | Judge one recorded action. Body: `{"mandate_id": "...", "seq": 1}`. Leave out `seq` for the latest. Add `"revocations": [...]` if the user revoked | 201 |
| `GET /v1/disputes/{id}` | The verdict and its status | 200 |
| `GET /v1/disputes/{id}/packet` | The evidence packet | 200 |
| `GET /v1/chains/{mandate_id}` | The exported bundle, for checking offline | 200 |
| `GET /v1/verify?mandate_id=...` | The witness checks its own chain | 200 |
| `POST /v1/dev/mandates` | Dev only: create, sign and register a mandate | 201 |
| `POST /v1/dev/revocations` | Dev only: sign a revocation for a mandate made that way | 201 |

The action fields are `merchant` (or `merchant_id`), `amount`, and either `description` or `cart`. Also send `category` if the mandate limits categories, and `step_up_done` if the user confirmed a large purchase. The witness stamps the time itself and ignores any time the client sends.

## Errors

| Code | Meaning |
|---|---|
| 400 | Malformed input. The message names the field |
| 401 | Missing or wrong bearer token |
| 403 | The mandate's user key is not on the allow-list |
| 404 | Unknown mandate, dispute or path |
| 409 | A different mandate is already registered under that id |
| 413 | The request body is over 1 MB |
| 422 | The mandate's signature does not verify |

## What to trust

- **Pin both keys when you verify an exported chain.** Take the user's key from the user's wallet and the witness's key from the witness's operator, then pass both to `verify_bundle`. Without them a check proves only that the bundle is internally consistent. See [TRUST-MODEL.md](TRUST-MODEL.md).
- **`/healthz` reports the witness key for convenience.** A key you get from the server you are checking is not an independent source.
- **Dev mode defeats the point of a user-held key,** because the server holds it. Use it for testing only.
- **Without `--principal-key`,** the first key to register a mandate is trusted as its user.
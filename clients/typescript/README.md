# Evenhand for TypeScript

A client for the [Evenhand witness](../../docs/HTTP-API.md) and an offline verifier, with no runtime dependencies. It needs Node 22 or newer.

Two jobs:

1. **Wrap an agent's purchase function** so that every purchase is recorded by the witness, and ask for a verdict, an evidence packet or a checkpoint when a purchase is disputed.
2. **Verify a chain offline**, without a server and without Python, against keys you pinned.

## Install

This is not on npm yet. From a checkout of the repository:

```
cd clients/typescript
npm install
npm run build
```

To use it from another project, run `npm install /path/to/evenhand/clients/typescript` there.

## Wrap your agent's purchase function

```ts
import { EvenhandClient } from 'evenhand';

const witness = new EvenhandClient('http://127.0.0.1:8787');

const buy = witness.tool('m-1', async (order: { merchant: string; amount: number; item: string }) => {
  return await shop.checkout(order); // your real purchase call
});

await buy({ merchant: 'stridemart', amount: 89, item: 'road running shoes' });
```

Every call that succeeds is recorded, and your function's own result is returned unchanged. A call that throws records nothing.

- If recording fails after the purchase went through, `buy` throws a `WitnessError` that carries the purchase's result in `error.result`. Pass `{ onWitnessError: 'ignore' }` to return the result quietly instead.
- By default the first argument must be an object with `merchant` and `amount`. For any other signature pass `extract`, which runs before the purchase, so a mistake in it stops the call before any money moves:

```ts
const pay = witness.tool('m-1', (merchant: string, cents: number) => processor.charge(merchant, cents), {
  extract: (merchant, cents) => ({ merchant, amount: cents / 100 }),
});
```

## Record, judge and checkpoint

```ts
const receipt = await witness.record('m-1', { merchant: 'stridemart', amount: 89, description: 'road running shoes' });

const dispute = await witness.dispute('m-1'); // the latest purchase, or { seq: 0 }
console.log(dispute.verdict.type); // 'within_mandate', 'exceeded_mandate', 'ambiguous_intent', ...
const packet = await witness.disputePacket(dispute.dispute_id);

const checkpoint = await witness.checkpoint('m-1'); // keep this where the witness cannot reach it
```

A checkpoint is the witness's signed statement that a chain held N receipts, the last with a given hash. It is what lets you catch receipts cut off the end of a chain later.

## Verify offline

```ts
const result = await witness.verifyChain('m-1', {
  witnessKey: WITNESS_PUBLIC_KEY, // published by the witness operator, not taken from the witness you are checking
  userKey: USER_PUBLIC_KEY, //       from the user's wallet
  checkpoint: savedCheckpoint, //    optional, but it is what catches truncation
});
if (!result.ok) console.error(result.problems);
```

Without pinned keys a check proves only that a chain is consistent with itself. See the [trust model](../../docs/TRUST-MODEL.md).

To verify a chain you already hold, pass the exact JSON text to `verifyBundle(text, { witnessKey, userKey })` or `verifyBundleWithCheckpoint(text, checkpoint, { witnessKey })`. Pass text, not a parsed object. The witness signs the text of every number, and JavaScript's `JSON.parse` turns `99.0` into `99`, which would make a genuine chain fail. `exportChain()` returns the text for you.

## Errors

| Error | When |
|---|---|
| `EvenhandError` with `status` 400 to 503 | The witness answered with an error. `message` is its explanation |
| `EvenhandError` with `status` 0 | The witness could not be reached, did not answer in time (default 10 seconds, `timeoutMs`), or sent something that is not JSON |
| `WitnessError` | A wrapped purchase went through but could not be recorded. `result` holds what the purchase returned |
| `TypeError` | `tool()` could not work out what to record, before the purchase ran |

## What this does not do

- It does not sign mandates. A user's wallet does that. For testing, `devMandate()` asks a witness started with `--dev` to make one with a throwaway key.
- It does not judge disputes itself. The witness does, and `dispute()` asks it.
- It is not built for browsers. It uses Node's crypto module.

## Tests

```
npm test
```

The tests start the real Python witness and check the TypeScript verifier against Python on thousands of generated cases, so they need Python with the `evenhand` package importable (run `pip install -e .` at the repository root). Set the `PYTHON` environment variable to choose an interpreter.
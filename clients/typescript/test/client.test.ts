import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { after, before, describe, test } from 'node:test';
import { EvenhandClient, EvenhandError, WitnessError, parseLossless, verifyBundle, verifyBundleWithCheckpoint } from '../src/index.js';
import type { Json } from '../src/index.js';
import { startServer, stringifyLossless, type RunningServer } from './helpers.js';

// ---------- against a fake network: URLs, headers, errors and the purchase wrapper ----------
interface Seen {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: unknown;
}

function fake(reply: (seen: Seen) => { status?: number; json?: unknown; text?: string }): { fetch: typeof fetch; calls: Seen[] } {
  const calls: Seen[] = [];
  const f = (async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const seen: Seen = {
      url: String(input),
      method: init?.method ?? 'GET',
      headers: { ...(init?.headers as Record<string, string>) },
      body: init?.body === undefined || init.body === null ? undefined : JSON.parse(String(init.body)),
    };
    calls.push(seen);
    const r = reply(seen);
    return new Response(r.text ?? JSON.stringify(r.json ?? {}), { status: r.status ?? 200 });
  }) as typeof fetch;
  return { fetch: f, calls };
}

describe('the client, against a fake network', () => {
  test('builds the right requests', async () => {
    const { fetch: f, calls } = fake(() => ({ json: {} }));
    const client = new EvenhandClient('http://witness.example:8787///', { fetch: f });
    await client.health();
    await client.registerMandate({ mandate: {} });
    await client.record('m-1', { merchant: 'stridemart', amount: 30, description: 'shoes' });
    await client.dispute('m-1');
    await client.dispute('m-1', { seq: 2 });
    await client.disputeStatus('d-0001');
    await client.disputePacket('d-0001');
    await client.verifyOnServer('m 1/x');
    await client.exportChain('m-1').catch(() => undefined);
    assert.deepEqual(
      calls.map((c) => `${c.method} ${c.url}`),
      [
        'GET http://witness.example:8787/healthz',
        'POST http://witness.example:8787/v1/mandates',
        'POST http://witness.example:8787/v1/receipts',
        'POST http://witness.example:8787/v1/disputes',
        'POST http://witness.example:8787/v1/disputes',
        'GET http://witness.example:8787/v1/disputes/d-0001',
        'GET http://witness.example:8787/v1/disputes/d-0001/packet',
        'GET http://witness.example:8787/v1/verify?mandate_id=m%201%2Fx',
        'GET http://witness.example:8787/v1/chains/m-1',
      ],
    );
    assert.deepEqual(calls[2]?.body, { mandate_id: 'm-1', action: { merchant: 'stridemart', amount: 30, description: 'shoes' } });
    assert.deepEqual(calls[3]?.body, { mandate_id: 'm-1' });
    assert.deepEqual(calls[4]?.body, { mandate_id: 'm-1', seq: 2 });
    assert.equal(calls[0]?.headers['Authorization'], undefined);
    assert.equal(calls[2]?.headers['Content-Type'], 'application/json');
  });

  test('sends the bearer token when given one', async () => {
    const { fetch: f, calls } = fake(() => ({ json: {} }));
    await new EvenhandClient('http://x', { fetch: f, apiKey: 's3cret' }).health();
    assert.equal(calls[0]?.headers['Authorization'], 'Bearer s3cret');
  });

  test('refuses a base URL that is not http or https', () => {
    assert.throws(() => new EvenhandClient('witness.example'), TypeError);
    assert.throws(() => new EvenhandClient('file:///etc/passwd'), TypeError);
  });

  test("turns the witness's errors into EvenhandError", async () => {
    const { fetch: f } = fake((s) => (s.url.endsWith('/healthz') ? { status: 401, json: { error: 'missing or wrong bearer token' } } : { status: 502, text: '<html>bad gateway</html>' }));
    const client = new EvenhandClient('http://x', { fetch: f });
    await assert.rejects(client.health(), (e: unknown) => e instanceof EvenhandError && e.status === 401 && e.message === 'missing or wrong bearer token');
    await assert.rejects(client.record('m', { merchant: 'a', amount: 1 }), (e: unknown) => e instanceof EvenhandError && e.status === 502 && e.message === 'HTTP 502');
  });

  test('reports an unreachable witness, a timeout and a reply that is not JSON', async () => {
    const down = new EvenhandClient('http://x', { fetch: (async () => { throw new TypeError('fetch failed'); }) as typeof fetch });
    await assert.rejects(down.health(), (e: unknown) => e instanceof EvenhandError && e.status === 0 && /could not reach the witness/.test(e.message));
    const hang = ((_url: unknown, init?: RequestInit) =>
      new Promise((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => reject(init.signal?.reason));
      })) as typeof fetch;
    await assert.rejects(new EvenhandClient('http://x', { fetch: hang, timeoutMs: 30 }).health(), (e: unknown) => e instanceof EvenhandError && e.status === 0 && /did not answer within 30 ms/.test(e.message));
    const junk = new EvenhandClient('http://x', { fetch: fake(() => ({ text: 'not json' })).fetch });
    await assert.rejects(junk.health(), (e: unknown) => e instanceof EvenhandError && /not JSON/.test(e.message));
  });

  test('does not trust a malformed checkpoint from the witness', async () => {
    const client = new EvenhandClient('http://x', { fetch: fake(() => ({ json: { format: 'evenhand-checkpoint-v1', count: 0 } })).fetch });
    await assert.rejects(client.checkpoint('m-1'), (e: unknown) => e instanceof EvenhandError && /malformed checkpoint/.test(e.message));
  });
});

describe('tool(), which wraps an agent\u2019s purchase function', () => {
  const receipt = { json: { seq: 0 } };

  test('records every successful call and returns the function\u2019s own result', async () => {
    const { fetch: f, calls } = fake(() => receipt);
    const client = new EvenhandClient('http://x', { fetch: f });
    const buy = client.tool('m-1', async (order: { merchant: string; amount: number; item: string }) => ({ orderId: 'o-1', ...order }));
    const result = await buy({ merchant: 'stridemart', amount: 30, item: 'road running shoes' });
    assert.equal(result.orderId, 'o-1');
    assert.equal(calls.length, 1);
    assert.deepEqual(calls[0]?.body, { mandate_id: 'm-1', action: { merchant: 'stridemart', amount: 30, description: 'road running shoes' } });
  });

  test('records nothing when the purchase throws', async () => {
    const { fetch: f, calls } = fake(() => receipt);
    const buy = new EvenhandClient('http://x', { fetch: f }).tool('m-1', async (_o: { merchant: string; amount: number }) => {
      throw new Error('card declined');
    });
    await assert.rejects(buy({ merchant: 'a', amount: 1 }), /card declined/);
    assert.equal(calls.length, 0);
  });

  test('stops before the purchase when it cannot work out what to record', async () => {
    const { fetch: f } = fake(() => receipt);
    let ran = false;
    const buy = new EvenhandClient('http://x', { fetch: f }).tool('m-1', (_x: string) => {
      ran = true;
      return 1;
    });
    await assert.rejects(buy('shoes'), TypeError);
    assert.equal(ran, false);
  });

  test('uses an extract function for any signature, including a synchronous one', async () => {
    const { fetch: f, calls } = fake(() => receipt);
    const buy = new EvenhandClient('http://x', { fetch: f }).tool('m-1', (merchant: string, cents: number) => `paid ${merchant}`, {
      extract: (merchant, cents) => ({ merchant, amount: cents / 100, category: 'sports', step_up_done: true }),
    });
    assert.equal(await buy('stridemart', 1999), 'paid stridemart');
    assert.deepEqual((calls[0]?.body as { action: unknown }).action, { merchant: 'stridemart', amount: 19.99, category: 'sports', step_up_done: true });
  });

  test('when recording fails after the purchase, says so and hands back the result', async () => {
    const { fetch: f } = fake(() => ({ status: 503, json: { error: 'could not save this' } }));
    const client = new EvenhandClient('http://x', { fetch: f });
    const buy = client.tool('m-1', async (_o: { merchant: string; amount: number }) => ({ orderId: 'o-9' }));
    await assert.rejects(buy({ merchant: 'a', amount: 1 }), (e: unknown) => e instanceof WitnessError && (e.result as { orderId: string }).orderId === 'o-9' && /could not save this/.test(e.message));
    const quiet = client.tool('m-1', async (_o: { merchant: string; amount: number }) => ({ orderId: 'o-10' }), { onWitnessError: 'ignore' });
    assert.deepEqual(await quiet({ merchant: 'a', amount: 1 }), { orderId: 'o-10' });
  });
});

// ---------- against the real Python witness ----------
describe('the client, against the real witness', () => {
  let server: RunningServer;
  let client: EvenhandClient;

  before(async () => {
    server = await startServer(['--dev']);
    client = new EvenhandClient(server.base);
  });
  after(async () => {
    await server.stop();
  });

  test('records, judges, checkpoints and verifies offline, end to end', async () => {
    const id = 'm-ts-1';
    const mandate = await client.devMandate({ intent: 'buy running shoes under $120', merchants: ['stridemart', 'caf\u00e9'], max_per_txn: 120, categories: ['sports'], mandate_id: id });
    const userKey = mandate.principal_pub;
    const witnessKey = server.witnessKey;
    assert.equal((await client.health()).witness_pub, witnessKey);
    assert.equal(mandate.witness_pub, witnessKey);

    // integers, decimals, non-ASCII text and quotes: each is a way for two languages to write a signed record differently
    assert.equal((await client.record(id, { merchant: 'stridemart', amount: 30, description: 'road running shoes', category: 'sports' })).seq, 0);
    await client.record(id, { merchant: 'caf\u00e9', amount: 0.1, description: 'caf\u00e9 au lait \u2615 \u{1f600}', category: 'sports' });
    await client.record(id, { merchant: 'stridemart', amount: 12.5, description: 'tab\there "quoted" \\ slash', category: 'sports' });

    const cp = await client.checkpoint(id);
    assert.equal(cp.count, 3);
    assert.deepEqual(await client.latestCheckpoint(id), cp);
    assert.deepEqual(await client.checkpoint(id), cp); // nothing new, so the same one

    const first = await client.verifyChain(id, { witnessKey, userKey, checkpoint: cp });
    assert.deepEqual(first.problems, []);
    assert.equal(first.receipts, 3);

    await client.record(id, { merchant: 'shadymart', amount: 89, description: 'running shoes', category: 'sports' });
    const grown = (await client.verifyChain(id, { witnessKey, userKey, checkpoint: cp })) as { ok: boolean; receiptsAfterCheckpoint: number };
    assert.equal(grown.ok, true);
    assert.equal(grown.receiptsAfterCheckpoint, 1);

    const latest = await client.dispute(id);
    assert.equal(latest.verdict.type, 'exceeded_mandate');
    assert.equal((await client.dispute(id, { seq: 0 })).verdict.type, 'within_mandate');
    assert.equal((await client.disputeStatus(latest.dispute_id)).verdict.type, 'exceeded_mandate');
    const packet = (await client.disputePacket(latest.dispute_id)) as { case_id?: string; verdict?: string };
    assert.equal(packet.case_id, latest.dispute_id);
    assert.equal(packet.verdict, 'exceeded_mandate');
    assert.equal((await client.verifyOnServer(id)).ok, true);
  });

  test('catches damage that the witness itself would not report', async () => {
    const id = 'm-ts-2';
    const mandate = await client.devMandate({ intent: 'buy running shoes under $120', merchants: 'stridemart', max_per_txn: 120, mandate_id: id });
    for (const amount of [30, 40, 50, 60]) await client.record(id, { merchant: 'stridemart', amount, description: 'road running shoes' });
    const cp = await client.checkpoint(id);
    const pinned = { witnessKey: server.witnessKey, userKey: mandate.principal_pub };
    const { text } = await client.exportChain(id);
    assert.equal(verifyBundle(text, pinned).ok, true);

    // an edited amount
    assert.notEqual(text.replace('"amount": 30,', '"amount": 3,'), text);
    assert.equal(verifyBundle(text.replace('"amount": 30,', '"amount": 3,'), pinned).ok, false);

    // receipts cut off the end: fine on its own, caught by the checkpoint
    const tree = parseLossless(text) as { [key: string]: Json };
    tree['receipts'] = (tree['receipts'] as Json[]).slice(0, 2);
    tree['head'] = null;
    const cut = stringifyLossless(tree);
    assert.equal(verifyBundle(cut, pinned).ok, true);
    const caught = verifyBundleWithCheckpoint(cut, cp, { witnessKey: server.witnessKey });
    assert.equal(caught.ok, false);
    assert.ok(caught.problems.some((p) => p.includes('cut off')));

    // the wrong witness, and the wrong user
    assert.equal(verifyBundle(text, { ...pinned, witnessKey: '00'.repeat(32) }).ok, false);
    assert.equal(verifyBundle(text, { ...pinned, userKey: '00'.repeat(32) }).ok, false);
  });

  test('reports the witness\u2019s errors', async () => {
    await client.devMandate({ intent: 'buy running shoes under $120', mandate_id: 'm-ts-empty' });
    await assert.rejects(client.record('m-ts-unknown', { merchant: 'a', amount: 1, description: 'x' }), (e: unknown) => e instanceof EvenhandError && e.status === 404);
    await assert.rejects(client.record('m-ts-empty', { merchant: 'a', amount: -5, description: 'x' }), (e: unknown) => e instanceof EvenhandError && e.status === 400);
    await assert.rejects(client.checkpoint('m-ts-empty'), (e: unknown) => e instanceof EvenhandError && e.status === 409);
    await assert.rejects(client.latestCheckpoint('m-ts-empty'), (e: unknown) => e instanceof EvenhandError && e.status === 404);
  });

  test('tool() records a real purchase function\u2019s calls', async () => {
    const id = 'm-ts-tool';
    const mandate = await client.devMandate({ intent: 'buy running shoes under $120', merchants: 'stridemart', max_per_txn: 120, mandate_id: id });
    let spent = 0;
    const buy = client.tool(id, async (order: { merchant: string; amount: number; item: string }) => {
      spent += order.amount;
      return `order-${spent}`;
    });
    assert.equal(await buy({ merchant: 'stridemart', amount: 30, item: 'road running shoes' }), 'order-30');
    assert.equal(await buy({ merchant: 'stridemart', amount: 45.5, item: 'road running shoes' }), 'order-75.5');
    const check = await client.verifyChain(id, { witnessKey: server.witnessKey, userKey: mandate.principal_pub });
    assert.equal(check.ok, true);
    assert.equal(check.receipts, 2);
  });
});

describe('the client, with a protected witness', () => {
  let server: RunningServer;
  before(async () => {
    server = await startServer(['--dev', '--api-key', 's3cret']);
  });
  after(async () => {
    await server.stop();
  });

  test('refuses requests without the key and accepts them with it', async () => {
    await assert.rejects(new EvenhandClient(server.base).devMandate({ intent: 'buy shoes' }), (e: unknown) => e instanceof EvenhandError && e.status === 401);
    const client = new EvenhandClient(server.base, { apiKey: 's3cret' });
    assert.equal((await client.devMandate({ intent: 'buy shoes', mandate_id: 'm-key' })).mandate_id, 'm-key');
  });
});

describe('the client, across a restart of a witness that saves its records', () => {
  let folder: string;
  before(() => {
    folder = mkdtempSync(path.join(tmpdir(), 'evenhand-ts-data-'));
  });
  after(() => {
    rmSync(folder, { recursive: true, force: true });
  });

  test('the key, the chain and the checkpoint survive, and a checkpoint saved earlier still verifies', async () => {
    const id = 'm-ts-restart';
    const first = await startServer(['--dev', '--data-dir', folder]);
    let cp;
    let userKey;
    try {
      const client = new EvenhandClient(first.base);
      userKey = (await client.devMandate({ intent: 'buy running shoes under $120', merchants: 'stridemart', max_per_txn: 120, mandate_id: id })).principal_pub;
      for (const amount of [30, 45.25]) await client.record(id, { merchant: 'stridemart', amount, description: 'road running shoes' });
      cp = await client.checkpoint(id);
    } finally {
      await first.stop();
    }
    const second = await startServer(['--dev', '--data-dir', folder]);
    try {
      assert.equal(second.witnessKey, first.witnessKey);
      const client = new EvenhandClient(second.base);
      assert.deepEqual(await client.latestCheckpoint(id), cp);
      assert.equal((await client.record(id, { merchant: 'stridemart', amount: 20, description: 'road running shoes' })).seq, 2);
      const result = await client.verifyChain(id, { witnessKey: first.witnessKey, userKey, checkpoint: cp });
      assert.deepEqual(result.problems, []);
      assert.equal(result.receipts, 3);
    } finally {
      await second.stop();
    }
  });
});
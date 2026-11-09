import assert from 'node:assert/strict';
import { test } from 'node:test';
import { verifyBundle, verifyBundleWithCheckpoint, verifyCheckpoint } from '../src/index.js';
import type { Checkpoint } from '../src/index.js';
import { fixtures } from './helpers.js';

interface BundleCase {
  name: string;
  text: string;
  witness_key: string;
  user_key: string;
  checkpoint: Checkpoint | null;
  python: { ok: boolean; head: string | null; receipts: number; malformed: boolean };
  python_checkpoint: { ok: boolean; count: number | null; after: number | null } | null;
}

const cases = fixtures<BundleCase>('bundles');

test('the corpus has valid, damaged and malformed bundles', () => {
  assert.ok(cases.length > 1000, `expected over a thousand cases, got ${cases.length}`);
  assert.ok(cases.filter((c) => c.python.ok).length >= 100);
  assert.ok(cases.filter((c) => !c.python.ok && !c.python.malformed).length >= 400);
  assert.ok(cases.filter((c) => c.python.malformed).length >= 300);
});

test('the verifier gives the same answer as Python on every case', () => {
  const failures: string[] = [];
  for (const c of cases) {
    const got = verifyBundle(c.text, { userKey: c.user_key, witnessKey: c.witness_key });
    if (got.ok !== c.python.ok) failures.push(`${c.name}: TypeScript ok=${got.ok}, Python ok=${c.python.ok}; ${got.problems.join('; ')}`);
    else if (c.python.ok && (got.head !== c.python.head || got.receipts !== c.python.receipts)) failures.push(`${c.name}: head or count differs`);
    else if (c.python.malformed !== got.problems.some((p) => p.startsWith('malformed bundle'))) failures.push(`${c.name}: disagrees on malformed`);
  }
  assert.deepEqual(failures.slice(0, 5), []);
});

test('with a checkpoint, the verifier gives the same answer as Python on every case', () => {
  let checked = 0;
  const failures: string[] = [];
  for (const c of cases) {
    if (c.checkpoint === null || c.python_checkpoint === null) continue;
    checked++;
    const got = verifyBundleWithCheckpoint(c.text, c.checkpoint, { witnessKey: c.witness_key, userKey: c.user_key });
    if (got.ok !== c.python_checkpoint.ok) failures.push(`${c.name}: TypeScript ok=${got.ok}, Python ok=${c.python_checkpoint.ok}; ${got.problems.join('; ')}`);
    else if (c.python_checkpoint.ok && (got.checkpointCount !== c.python_checkpoint.count || got.receiptsAfterCheckpoint !== c.python_checkpoint.after)) {
      failures.push(`${c.name}: checkpoint counts differ`);
    }
  }
  assert.ok(checked > 1000);
  assert.deepEqual(failures.slice(0, 5), []);
});

test('a truncated chain passes alone but fails against a saved checkpoint, as in Python', () => {
  const caught = cases.filter((c) => c.name.includes('drop last receipt') && c.python.ok && c.python_checkpoint !== null && !c.python_checkpoint.ok);
  assert.ok(caught.length >= 3, `expected several truncation cases, found ${caught.length}`);
  for (const c of caught) {
    assert.equal(verifyBundle(c.text, { userKey: c.user_key, witnessKey: c.witness_key }).ok, true, c.name);
    const got = verifyBundleWithCheckpoint(c.text, c.checkpoint as Checkpoint, { witnessKey: c.witness_key, userKey: c.user_key });
    assert.equal(got.ok, false, c.name);
    assert.ok(got.problems.some((p) => p.includes('cut off')), c.name);
  }
});

test('writing the same numbers differently breaks the signature, in both languages', () => {
  const changed = cases.filter((c) => c.name.includes('number text changed'));
  assert.ok(changed.length >= 10);
  for (const c of changed) {
    assert.equal(c.python.ok, false, c.name);
    assert.equal(verifyBundle(c.text, { userKey: c.user_key, witnessKey: c.witness_key }).ok, false, c.name);
  }
});

test('the verifier needs only the pinned keys, and rejects the wrong ones', () => {
  const valid = cases.find((c) => c.name.startsWith('valid ') && !c.name.includes('wrong') && c.python.ok);
  assert.ok(valid);
  const v = valid as BundleCase;
  assert.equal(verifyBundle(v.text).ok, true);
  assert.equal(verifyBundle(v.text, { witnessKey: '00'.repeat(32) }).ok, false);
  assert.equal(verifyBundle(v.text, { userKey: '00'.repeat(32) }).ok, false);
  assert.equal(verifyBundle(v.text, { head: '00'.repeat(32) }).ok, false);
  assert.equal(verifyBundle('not json').ok, false);
  assert.equal(verifyBundle('[]').problems[0]?.startsWith('malformed bundle'), true);
});

test('checkpoints verify alone, and only under the witness you pinned', () => {
  const c = cases.find((x) => x.checkpoint !== null && x.python.ok && x.python_checkpoint?.ok) as BundleCase;
  assert.ok(c);
  const cp = c.checkpoint as Checkpoint;
  assert.equal(verifyCheckpoint(cp), true);
  assert.equal(verifyCheckpoint(JSON.stringify(cp), c.witness_key), true);
  assert.equal(verifyCheckpoint(cp, '00'.repeat(32)), false);
  assert.equal(verifyCheckpoint({ ...cp, count: cp.count + 1 }), false);
  assert.equal(verifyCheckpoint('{"format":"evenhand-checkpoint-v1"}'), false);
  assert.equal(verifyCheckpoint('not json'), false);
  const bad = verifyBundleWithCheckpoint(c.text, { ...cp, head: '11'.repeat(32) }, { witnessKey: c.witness_key });
  assert.equal(bad.ok, false);
});
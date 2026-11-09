/**
 * Offline verification of an exported chain and of checkpoints, with no server and no Python.
 * It mirrors evenhand.verify_bundle and evenhand.checkpoint, including the wording of the problems it reports.
 *
 * Pass the exact JSON text the server sent (ExportedChain.text), not a parsed object: signatures cover the
 * text of every number, and a parsed object has already lost whether 99 was written as 99 or 99.0.
 */
import { createHash, createPublicKey, verify as nodeVerify, type KeyObject } from 'node:crypto';
import { canonicalBytes } from './canonical.js';
import { parseLossless, type Json } from './lossless.js';
import { SchemaError, parseBundle, parseCheckpoint, type CheckpointParsed, type ReceiptParsed } from './schema.js';
import type { Checkpoint, CheckpointVerifyResult, VerifyResult } from './types.js';

const GENESIS = '0'.repeat(64);

const keyCache = new Map<string, KeyObject | null>();

function publicKey(pubHex: string): KeyObject | null {
  const cached = keyCache.get(pubHex);
  if (cached !== undefined) return cached;
  let key: KeyObject | null = null;
  try {
    key = createPublicKey({ key: { kty: 'OKP', crv: 'Ed25519', x: Buffer.from(pubHex, 'hex').toString('base64url') }, format: 'jwk' });
  } catch {
    key = null;
  }
  if (keyCache.size > 256) keyCache.clear();
  keyCache.set(pubHex, key);
  return key;
}

/** True if sigHex is a valid Ed25519 signature of payload under the public key pubHex. Never throws. */
export function ed25519Verify(pubHex: string, payload: Uint8Array, sigHex: string): boolean {
  const key = publicKey(pubHex);
  if (key === null) return false;
  try {
    return nodeVerify(null, payload, key, Buffer.from(sigHex, 'hex'));
  } catch {
    return false;
  }
}

function sha256Hex(...parts: Uint8Array[]): string {
  const h = createHash('sha256');
  for (const p of parts) h.update(p);
  return h.digest('hex');
}

function receiptBody(r: ReceiptParsed): Json {
  return { seq: r.seqNum, prev_hash: r.prevHash, action: r.action.json, witness_pub: r.witnessPub };
}

function receiptHash(r: ReceiptParsed): string {
  return sha256Hex(canonicalBytes(receiptBody(r)), Buffer.from(r.signature, 'hex'));
}

export interface VerifyOptions {
  /** The user's public key, from the user's wallet. Not from the bundle. */
  userKey?: string;
  /** The witness's public key, published by the witness operator. Not from the bundle. */
  witnessKey?: string;
  /** A head hash the witness published. Catches truncation, but only exactly this head. Prefer a checkpoint. */
  head?: string;
}

function malformed(message: string): VerifyResult {
  return { ok: false, problems: [`malformed bundle: ${message}`], head: null, receipts: 0, principalPub: null, witnessPub: null };
}

/** The offline check of an exported chain. Without keys it proves only that the bundle is internally consistent. */
export function verifyBundle(bundleText: string, options: VerifyOptions = {}): VerifyResult {
  let bundle;
  try {
    bundle = parseBundle(parseLossless(bundleText));
  } catch (err) {
    if (err instanceof SchemaError || err instanceof SyntaxError) return malformed(err.message);
    throw err;
  }
  const { mandate, receipts, witnessPub, statedHead } = bundle;
  const problems: string[] = [];
  if (!ed25519Verify(mandate.principalPub, canonicalBytes(mandate.json), mandate.signature)) {
    problems.push('mandate signature does not verify');
  }
  if (options.userKey !== undefined && options.userKey !== mandate.principalPub) problems.push('mandate was signed by an unexpected key');
  if (options.witnessKey !== undefined && options.witnessKey !== witnessPub) problems.push('receipts were signed by an unexpected witness key');

  let prev = GENESIS;
  const hashes: string[] = [];
  receipts.forEach((r, i) => {
    const seq = r.seq.toString();
    if (r.witnessPub !== witnessPub) problems.push(`receipt ${seq}: signed by an unexpected witness key`);
    if (!ed25519Verify(witnessPub, canonicalBytes(receiptBody(r)), r.signature)) {
      problems.push(`receipt ${seq}: signature does not verify (content altered or forged)`);
    }
    if (r.seq !== BigInt(i)) problems.push(`position ${i} holds seq ${seq} (gap, deletion or reorder)`);
    if (r.prevHash !== prev) problems.push(`receipt ${seq}: broken hash link to previous receipt`);
    if (r.action.mandateId !== mandate.mandateId) problems.push(`receipt ${seq}: belongs to a different mandate`);
    prev = receiptHash(r);
    hashes.push(prev);
  });
  const head = receipts.length > 0 ? prev : null;
  if (options.head !== undefined && head !== options.head) {
    problems.push('chain head does not match the witness checkpoint (truncated or extended)');
  }
  receipts.forEach((r, i) => {
    if (r.statedHash !== undefined && (typeof r.statedHash !== 'string' || r.statedHash !== hashes[i])) {
      problems.push(`receipt ${r.seq.toString()}: stated hash does not match its contents`);
    }
  });
  if (statedHead !== null && statedHead !== head) problems.push('stated head does not match the last receipt');
  return { ok: problems.length === 0, problems, head, receipts: receipts.length, principalPub: mandate.principalPub, witnessPub };
}

// ---------- checkpoints ----------
function parseCheckpointInput(checkpoint: string | Checkpoint): CheckpointParsed {
  const text = typeof checkpoint === 'string' ? checkpoint : JSON.stringify(checkpoint);
  return parseCheckpoint(parseLossless(text));
}

function checkpointSignatureOk(cp: CheckpointParsed): boolean {
  const payload: Json = {
    type: 'checkpoint',
    mandate_id: cp.mandateId,
    count: cp.countNum,
    head: cp.head,
    issued_at: cp.issuedNum,
    witness_pub: cp.witnessPub,
  };
  return ed25519Verify(cp.witnessPub, canonicalBytes(payload), cp.signature);
}

/** True if the checkpoint is well formed and correctly signed, and, when given, signed by the witness you expect. */
export function verifyCheckpoint(checkpoint: string | Checkpoint, witnessKey?: string): boolean {
  try {
    const cp = parseCheckpointInput(checkpoint);
    if (witnessKey !== undefined && cp.witnessPub !== witnessKey) return false;
    return checkpointSignatureOk(cp);
  } catch (err) {
    if (err instanceof SchemaError || err instanceof SyntaxError) return false;
    throw err;
  }
}

export interface CheckpointVerifyOptions {
  /** Required: a checkpoint from a witness you did not choose proves nothing. */
  witnessKey: string;
  userKey?: string;
}

/**
 * verifyBundle, plus: the chain must still contain the receipt that a checkpoint you saved earlier covers.
 * This is what catches receipts cut off the end of a chain, or history rewritten.
 */
export function verifyBundleWithCheckpoint(
  bundleText: string,
  checkpoint: string | Checkpoint,
  options: CheckpointVerifyOptions,
): CheckpointVerifyResult {
  const result = verifyBundle(bundleText, { userKey: options.userKey, witnessKey: options.witnessKey });
  const problems = [...result.problems];
  const none = { checkpointCount: null, receiptsAfterCheckpoint: null };
  let cp: CheckpointParsed;
  try {
    cp = parseCheckpointInput(checkpoint);
  } catch (err) {
    if (err instanceof SchemaError || err instanceof SyntaxError) {
      problems.push(`malformed checkpoint: ${err.message}`);
      return { ...result, ok: false, problems, ...none };
    }
    throw err;
  }
  if (cp.witnessPub !== options.witnessKey) {
    problems.push('the checkpoint was signed by a different witness key than the one you expected');
  } else if (!checkpointSignatureOk(cp)) {
    problems.push("the checkpoint's signature does not verify");
  }
  let bundle;
  try {
    bundle = parseBundle(parseLossless(bundleText));
  } catch (err) {
    if (err instanceof SchemaError || err instanceof SyntaxError) return { ...result, ok: false, problems, ...none };
    throw err;
  }
  const receipts = bundle.receipts;
  if (cp.mandateId !== bundle.mandate.mandateId) {
    problems.push('the checkpoint is for a different mandate than this bundle');
  } else if (BigInt(receipts.length) < cp.count) {
    problems.push(`the chain has ${receipts.length} receipts but the checkpoint covers ${cp.count}: receipts were cut off`);
  } else if (receiptHash(receipts[Number(cp.count) - 1] as ReceiptParsed) !== cp.head) {
    problems.push(`receipt ${cp.count - 1n} is not the one the checkpoint covers: history was rewritten`);
  }
  const covered = Number(cp.count);
  return {
    ...result,
    ok: problems.length === 0,
    problems,
    checkpointCount: covered,
    receiptsAfterCheckpoint: Math.max(0, receipts.length - covered),
  };
}
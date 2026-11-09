/**
 * Reads mandates, receipts, bundles and checkpoints from JSON with the same strict rules as the Python side
 * (evenhand/serialize.py), keeping every number's original text so that signatures can be checked.
 * Anything malformed throws a SchemaError that names the field.
 */
import { Num, type Json } from './lossless.js';

export class SchemaError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'SchemaError';
  }
}

type Obj = { [key: string]: Json };

export const KEY_HEX = 64;
export const SIG_HEX = 128;
export const HASH_HEX = 64;
export const BUNDLE_FORMAT = 'evenhand-bundle-v1';
export const CHECKPOINT_FORMAT = 'evenhand-checkpoint-v1';

function obj(d: Json | undefined, where: string): Obj {
  if (d === null || d === undefined || typeof d !== 'object' || Array.isArray(d) || d instanceof Num) {
    throw new SchemaError(`${where} must be a JSON object`);
  }
  return d as Obj;
}

function get(d: Obj, key: string, where: string): Json {
  if (!Object.hasOwn(d, key)) throw new SchemaError(`${where}.${key} is required`);
  return d[key] as Json;
}

function str(d: Obj, key: string, where: string, allowEmpty = false): string {
  const v = get(d, key, where);
  if (typeof v !== 'string' || (v === '' && !allowEmpty)) throw new SchemaError(`${where}.${key} must be a non-empty string`);
  return v;
}

function num(d: Obj, key: string, where: string, nullable: boolean, minimum?: number): Num | null {
  const v = get(d, key, where);
  if (v === null && nullable) return null;
  if (!(v instanceof Num) || !Number.isFinite(v.value)) {
    throw new SchemaError(`${where}.${key} must be a finite number${nullable ? ' or null' : ''}`);
  }
  if (minimum !== undefined && v.value < minimum) throw new SchemaError(`${where}.${key} must be at least ${minimum}`);
  return v;
}

function requiredNum(d: Obj, key: string, where: string, minimum?: number): Num {
  return num(d, key, where, false, minimum) as Num;
}

function int(d: Obj, key: string, where: string, minimum: number): Num {
  const v = get(d, key, where);
  if (!(v instanceof Num) || v.isFloat || BigInt(v.lexeme) < BigInt(minimum)) {
    throw new SchemaError(`${where}.${key} must be an integer of at least ${minimum}`);
  }
  return v;
}

function bool(d: Obj, key: string, where: string): boolean {
  const v = get(d, key, where);
  if (typeof v !== 'boolean') throw new SchemaError(`${where}.${key} must be true or false`);
  return v;
}

function hex(d: Obj, key: string, where: string, length: number): string {
  const v = get(d, key, where);
  if (typeof v !== 'string' || v.length !== length || !/^[0-9a-f]*$/.test(v)) {
    throw new SchemaError(`${where}.${key} must be ${length} lowercase hex characters`);
  }
  return v;
}

function strs(d: Obj, key: string, where: string, nullable: boolean): string[] | null {
  const v = get(d, key, where);
  if (v === null && nullable) return null;
  if (!Array.isArray(v) || v.some((x) => typeof x !== 'string')) {
    throw new SchemaError(`${where}.${key} must be a list of strings${nullable ? ' or null' : ''}`);
  }
  return v as string[];
}

function requiredStrs(d: Obj, key: string, where: string): string[] {
  return strs(d, key, where, false) as string[];
}

// ---------- mandates ----------
export interface MandateParsed {
  /** The mandate rebuilt in the shape Python signs, ready for canonical JSON. */
  json: Json;
  mandateId: string;
}

function parseMandate(d: Json | undefined): MandateParsed {
  const w = 'mandate';
  const m = obj(d, w);
  const cw = 'mandate.constraints';
  const c = obj(get(m, 'constraints', w), cw);
  const constraints: Json = {
    max_per_txn: num(c, 'max_per_txn', cw, true, 0),
    max_total: num(c, 'max_total', cw, true, 0),
    currency: str(c, 'currency', cw),
    merchants_allow: strs(c, 'merchants_allow', cw, true),
    merchants_deny: requiredStrs(c, 'merchants_deny', cw),
    categories_allow: strs(c, 'categories_allow', cw, true),
    step_up_above: num(c, 'step_up_above', cw, true, 0),
  };
  const mandateId = str(m, 'mandate_id', w);
  const json: Json = {
    mandate_id: mandateId,
    principal_id: str(m, 'principal_id', w),
    agent_id: str(m, 'agent_id', w),
    issued_at: requiredNum(m, 'issued_at', w),
    expires_at: requiredNum(m, 'expires_at', w),
    actions: requiredStrs(m, 'actions', w),
    constraints,
    intent: str(m, 'intent', w, true),
  };
  return { json, mandateId };
}

export interface SignedMandateParsed extends MandateParsed {
  principalPub: string;
  signature: string;
}

export function parseSignedMandate(d: Json | undefined): SignedMandateParsed {
  const w = 'signed_mandate';
  const s = obj(d, w);
  const mandate = parseMandate(get(s, 'mandate', w));
  return { ...mandate, principalPub: hex(s, 'principal_pub', w, KEY_HEX), signature: hex(s, 'signature', w, SIG_HEX) };
}

/** Checks a revocation's shape. Evenhand's offline check does not use revocations, but a malformed one makes a bundle malformed. */
export function parseRevocation(d: Json | undefined): void {
  const w = 'revocation';
  const r = obj(d, w);
  str(r, 'mandate_id', w);
  requiredNum(r, 'revoked_at', w);
  hex(r, 'principal_pub', w, KEY_HEX);
  hex(r, 'signature', w, SIG_HEX);
}

// ---------- actions and receipts ----------
export interface ActionParsed {
  json: Json;
  mandateId: string;
}

export function parseAction(d: Json | undefined, where: string): ActionParsed {
  const a = obj(d, where);
  const cartRaw = get(a, 'cart', where);
  if (!Array.isArray(cartRaw) || cartRaw.length === 0) throw new SchemaError(`${where}.cart must be a non-empty list`);
  const cart: Json[] = cartRaw.map((item, i) => {
    const cw = `${where}.cart[${i}]`;
    const it = obj(item, cw);
    return { sku: str(it, 'sku', cw), desc: str(it, 'desc', cw, true), price: requiredNum(it, 'price', cw, 0), qty: int(it, 'qty', cw, 1) };
  });
  const mandateId = str(a, 'mandate_id', where);
  const json: Json = {
    mandate_id: mandateId,
    agent_id: str(a, 'agent_id', where),
    action: str(a, 'action', where),
    amount: requiredNum(a, 'amount', where, 0),
    currency: str(a, 'currency', where),
    merchant_id: str(a, 'merchant_id', where),
    category: str(a, 'category', where),
    cart,
    step_up_done: bool(a, 'step_up_done', where),
    ts: requiredNum(a, 'ts', where),
  };
  return { json, mandateId };
}

export interface ReceiptParsed {
  seqNum: Num;
  seq: bigint;
  prevHash: string;
  action: ActionParsed;
  witnessPub: string;
  signature: string;
  /** The "hash" the sender stated, if any. It is derived data, so it is recomputed and only compared. */
  statedHash: Json | undefined;
}

export function parseReceipt(d: Json | undefined): ReceiptParsed {
  const w = 'receipt';
  const r = obj(d, w);
  const seqNum = int(r, 'seq', w, 0);
  return {
    seqNum,
    seq: BigInt(seqNum.lexeme),
    prevHash: hex(r, 'prev_hash', w, HASH_HEX),
    action: parseAction(get(r, 'action', w), 'receipt.action'),
    witnessPub: hex(r, 'witness_pub', w, KEY_HEX),
    signature: hex(r, 'signature', w, SIG_HEX),
    statedHash: Object.hasOwn(r, 'hash') ? (r['hash'] as Json) : undefined,
  };
}

// ---------- bundles ----------
export interface BundleParsed {
  mandate: SignedMandateParsed;
  receipts: ReceiptParsed[];
  witnessPub: string;
  statedHead: string | null;
}

export function parseBundle(d: Json): BundleParsed {
  const w = 'bundle';
  const b = obj(d, w);
  if (b['format'] !== BUNDLE_FORMAT) throw new SchemaError(`bundle.format must be '${BUNDLE_FORMAT}'`);
  const receiptsRaw = get(b, 'receipts', w);
  if (!Array.isArray(receiptsRaw)) throw new SchemaError('bundle.receipts must be a list');
  const revocationsRaw: Json = Object.hasOwn(b, 'revocations') ? (b['revocations'] as Json) : [];
  if (!Array.isArray(revocationsRaw)) throw new SchemaError('bundle.revocations must be a list');
  const head: Json = Object.hasOwn(b, 'head') ? (b['head'] as Json) : null;
  if (head !== null && (typeof head !== 'string' || head.length !== HASH_HEX)) {
    throw new SchemaError('bundle.head must be null or a SHA-256 hex string');
  }
  const mandate = parseSignedMandate(get(b, 'mandate', w));
  const receipts = receiptsRaw.map(parseReceipt);
  const witnessPub = hex(b, 'witness_pub', w, KEY_HEX);
  revocationsRaw.forEach(parseRevocation);
  return { mandate, receipts, witnessPub, statedHead: head };
}

// ---------- checkpoints ----------
export interface CheckpointParsed {
  mandateId: string;
  countNum: Num;
  count: bigint;
  head: string;
  issuedNum: Num;
  witnessPub: string;
  signature: string;
}

export function parseCheckpoint(d: Json): CheckpointParsed {
  const w = 'checkpoint';
  const c = obj(d, w);
  if (c['format'] !== CHECKPOINT_FORMAT) throw new SchemaError(`checkpoint.format must be '${CHECKPOINT_FORMAT}'`);
  const countNum = int(c, 'count', w, 1);
  return {
    mandateId: str(c, 'mandate_id', w),
    countNum,
    count: BigInt(countNum.lexeme),
    head: hex(c, 'head', w, HASH_HEX),
    issuedNum: int(c, 'issued_at', w, 0),
    witnessPub: hex(c, 'witness_pub', w, KEY_HEX),
    signature: hex(c, 'signature', w, SIG_HEX),
  };
}
/**
 * Canonical JSON, byte for byte the same as the Python side:
 *   json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
 * Evenhand signs these bytes, so any difference here makes a genuine signature fail.
 */
import { Num, type Json } from './lossless.js';

/** Python's repr() of a float: the shortest digits that round-trip, in Python's choice of fixed or exponent form. */
export function pyFloatRepr(x: number): string {
  if (!Number.isFinite(x)) throw new Error('a non-finite number cannot be written as canonical JSON');
  if (x === 0) return Object.is(x, -0) ? '-0.0' : '0.0';
  const sign = x < 0 ? '-' : '';
  const [mantissa, exponentText] = Math.abs(x).toExponential().split('e') as [string, string];
  const digits = mantissa.replace('.', '');
  const exponent = parseInt(exponentText, 10);
  const pointAt = exponent + 1; // the value is 0.DIGITS times 10 to the power pointAt
  if (pointAt > 16 || pointAt <= -4) {
    const body = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    const absExp = Math.abs(exponent);
    return `${sign}${body}e${exponent < 0 ? '-' : '+'}${absExp < 10 ? '0' : ''}${absExp}`;
  }
  if (pointAt <= 0) return `${sign}0.${'0'.repeat(-pointAt)}${digits}`;
  if (pointAt >= digits.length) return `${sign}${digits}${'0'.repeat(pointAt - digits.length)}.0`;
  return `${sign}${digits.slice(0, pointAt)}.${digits.slice(pointAt)}`;
}

const SHORT_ESCAPES: { [ch: string]: string } = {
  '"': '\\"',
  '\\': '\\\\',
  '\b': '\\b',
  '\f': '\\f',
  '\n': '\\n',
  '\r': '\\r',
  '\t': '\\t',
};

/** A JSON string literal the way Python writes it with ensure_ascii: everything outside space to tilde is escaped. */
export function pyString(s: string): string {
  let out = '"';
  for (let i = 0; i < s.length; i++) {
    const ch = s.charAt(i);
    const code = s.charCodeAt(i);
    const short = SHORT_ESCAPES[ch];
    if (short !== undefined) out += short;
    else if (code >= 0x20 && code <= 0x7e) out += ch;
    else out += '\\u' + code.toString(16).padStart(4, '0');
  }
  return out + '"';
}

/** Python sorts keys by Unicode code point. JavaScript's default sorts by UTF-16 unit, which differs above U+FFFF. */
function compareCodePoints(a: string, b: string): number {
  const left = a[Symbol.iterator]();
  const right = b[Symbol.iterator]();
  for (;;) {
    const x = left.next();
    const y = right.next();
    if (x.done || y.done) return x.done === y.done ? 0 : x.done ? -1 : 1;
    const cx = x.value.codePointAt(0) as number;
    const cy = y.value.codePointAt(0) as number;
    if (cx !== cy) return cx - cy;
  }
}

function canonicalNumber(n: Num): string {
  return n.isFloat ? pyFloatRepr(n.value) : BigInt(n.lexeme).toString();
}

export function canonicalString(v: Json): string {
  if (v === null) return 'null';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'string') return pyString(v);
  if (v instanceof Num) return canonicalNumber(v);
  if (Array.isArray(v)) return '[' + v.map(canonicalString).join(',') + ']';
  const keys = Object.keys(v).sort(compareCodePoints);
  return '{' + keys.map((k) => `${pyString(k)}:${canonicalString(v[k] as Json)}`).join(',') + '}';
}

export function canonicalBytes(v: Json): Buffer {
  return Buffer.from(canonicalString(v), 'utf8');
}
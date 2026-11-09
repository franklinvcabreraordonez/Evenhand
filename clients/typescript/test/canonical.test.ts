import assert from 'node:assert/strict';
import { test } from 'node:test';
import { canonicalString, parseLossless, pyFloatRepr, pyString } from '../src/index.js';
import { fixtures } from './helpers.js';

interface Case {
  kind: 'float' | 'int' | 'string' | 'object';
  text: string;
  canonical: string;
}

const cases = fixtures<Case>('canonical');

test('canonical JSON is byte for byte what Python writes, for every number, string and object', () => {
  assert.ok(cases.length > 6000, `expected thousands of cases, got ${cases.length}`);
  const failures: string[] = [];
  for (const c of cases) {
    const got = canonicalString(parseLossless(c.text));
    if (got !== c.canonical) failures.push(`${c.kind} ${c.text}: got ${got}, Python wrote ${c.canonical}`);
  }
  assert.deepEqual(failures.slice(0, 5), []);
});

test('the corpus covers each kind of value', () => {
  const kinds = new Set(cases.map((c) => c.kind));
  assert.deepEqual([...kinds].sort(), ['float', 'int', 'object', 'string']);
});

test('99 and 99.0 stay different, because Python signs them differently', () => {
  assert.equal(canonicalString(parseLossless('{"a":99}')), '{"a":99}');
  assert.equal(canonicalString(parseLossless('{"a":99.0}')), '{"a":99.0}');
  assert.equal(canonicalString(parseLossless('{"a": 1E2}')), '{"a":100.0}');
  assert.equal(canonicalString(parseLossless('-0')), '0');
  assert.equal(canonicalString(parseLossless('-0.0')), '-0.0');
  assert.equal(canonicalString(parseLossless('12345678901234567890')), '12345678901234567890');
});

test('floats print the way Python prints them', () => {
  const known: Array<[number, string]> = [
    [1e16, '1e+16'],
    [1e15, '1000000000000000.0'],
    [1e-5, '1e-05'],
    [0.0001, '0.0001'],
    [5e-324, '5e-324'],
    [0.1, '0.1'],
    [12345.5, '12345.5'],
    [100, '100.0'],
    [1.5e300, '1.5e+300'],
  ];
  for (const [value, text] of known) assert.equal(pyFloatRepr(value), text);
  assert.throws(() => pyFloatRepr(Infinity));
});

test('strings escape everything outside space to tilde, as Python does with ensure_ascii', () => {
  assert.equal(pyString('caf\u00e9'), '"caf\\u00e9"');
  assert.equal(pyString('\u{1f600}'), '"\\ud83d\\ude00"');
  assert.equal(pyString('a"b\\c\n\t\u007f\u0001'), '"a\\"b\\\\c\\n\\t\\u007f\\u0001"');
});

test('keys sort by code point, not by UTF-16 unit', () => {
  // U+FFFF sorts before U+10000 by code point, but after it by UTF-16 unit (the high surrogate is 0xD800).
  const text = JSON.stringify({ '\u{10000}': 1, '\uffff': 2 });
  assert.equal(canonicalString(parseLossless(text)), '{"\\uffff":2,"\\ud800\\udc00":1}');
});
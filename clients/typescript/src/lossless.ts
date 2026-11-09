/**
 * Reads JSON without losing how each number was written.
 *
 * Evenhand's signatures cover the exact text of a record, and Python prints 99 and 99.0 differently.
 * JavaScript's JSON.parse turns both into 99, so a verifier that used it would reject genuine records.
 * Node 22 can hand us the original text of every number, and this keeps it.
 */

/** A JSON number that remembers its original text. */
export class Num {
  readonly lexeme: string;
  readonly value: number;

  constructor(lexeme: string, value: number) {
    this.lexeme = lexeme;
    this.value = value;
  }

  /** True when the text has a decimal point or an exponent. Python reads those as floats, everything else as ints. */
  get isFloat(): boolean {
    return /[.eE]/.test(this.lexeme);
  }
}

export type Json = null | boolean | string | Num | Json[] | { [key: string]: Json };

/** Parses JSON text, keeping every number as a Num. Throws on invalid JSON, or on a Node too old to give number text. */
export function parseLossless(text: string): Json {
  let sawMissingSource = false;
  const reviver = function (this: unknown, _key: string, value: unknown, context?: { source?: string }): unknown {
    if (typeof value === 'number') {
      if (context === undefined || typeof context.source !== 'string') {
        sawMissingSource = true;
        return value;
      }
      return new Num(context.source, value);
    }
    return value;
  };
  const parsed: unknown = JSON.parse(text, reviver as (this: any, key: string, value: any) => any);
  if (sawMissingSource) {
    throw new Error('this version of Node does not expose the original text of JSON numbers; use Node 22 or newer');
  }
  return parsed as Json;
}
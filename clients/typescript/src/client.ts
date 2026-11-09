import { parseLossless } from './lossless.js';
import { SchemaError, parseCheckpoint } from './schema.js';
import type {
  ActionInput,
  Checkpoint,
  CheckpointVerifyResult,
  DevMandate,
  DevMandateInput,
  Dispute,
  ExportedChain,
  Health,
  MandateRegistration,
  Receipt,
  Revocation,
  ServerVerification,
  VerifyResult,
} from './types.js';
import { verifyBundle, verifyBundleWithCheckpoint } from './verify.js';

/** The witness answered with an error, could not be reached, or sent something unreadable. */
export class EvenhandError extends Error {
  /** The HTTP status, or 0 when no usable reply arrived. */
  readonly status: number;
  /** The reply body, when there was one. */
  readonly body: unknown;

  constructor(message: string, status: number, body?: unknown, options?: { cause?: unknown }) {
    super(message, options);
    this.name = 'EvenhandError';
    this.status = status;
    this.body = body;
  }
}

/** The purchase went through, but the witness could not record it. The purchase's own result is attached. */
export class WitnessError extends Error {
  readonly result: unknown;

  constructor(message: string, result: unknown, options?: { cause?: unknown }) {
    super(message, options);
    this.name = 'WitnessError';
    this.result = result;
  }
}

export interface ClientOptions {
  /** Sent as "Authorization: Bearer ..." when the witness was started with --api-key. */
  apiKey?: string;
  /** How long to wait for each reply. Default 10 seconds. */
  timeoutMs?: number;
  /** Replace the global fetch, for tests or a proxy. */
  fetch?: typeof fetch;
}

export interface ToolOptions<A extends unknown[]> {
  /**
   * Turns the call's arguments into the action to record. It runs before the purchase, so a mistake here
   * stops the call before any money moves. By default the first argument must be an object with
   * merchant (or merchant_id) and amount (or price or total).
   */
  extract?: (...args: A) => ActionInput;
  /**
   * What to do when the purchase succeeded but recording it failed. "throw" (the default) raises a
   * WitnessError that carries the purchase's result. "ignore" returns the result and says nothing.
   */
  onWitnessError?: 'throw' | 'ignore';
}

function defaultExtract(args: unknown[]): ActionInput {
  const first = args[0];
  if (typeof first === 'object' && first !== null) {
    const o = first as Record<string, unknown>;
    const merchant = o['merchant'] ?? o['merchant_id'];
    const amount = o['amount'] ?? o['price'] ?? o['total'];
    if (typeof merchant === 'string' && typeof amount === 'number') {
      const action: ActionInput = { merchant, amount };
      const description = o['description'] ?? o['item'] ?? o['desc'] ?? o['product'];
      if (typeof description === 'string') action.description = description;
      if (typeof o['category'] === 'string') action.category = o['category'];
      if (typeof o['sku'] === 'string') action.sku = o['sku'];
      if (o['step_up_done'] === true || o['confirmed'] === true) action.step_up_done = true;
      return action;
    }
  }
  throw new TypeError(
    'could not find the merchant and amount in this call: pass an object with merchant and amount as the first argument, or give tool() an extract function',
  );
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

export class EvenhandClient {
  private readonly base: string;
  private readonly apiKey: string | undefined;
  private readonly timeoutMs: number;
  private readonly fetchFn: typeof fetch;

  constructor(baseUrl: string, options: ClientOptions = {}) {
    if (!/^https?:\/\//i.test(baseUrl)) throw new TypeError('baseUrl must start with http:// or https://');
    this.base = baseUrl.replace(/\/+$/, '');
    this.apiKey = options.apiKey;
    this.timeoutMs = options.timeoutMs ?? 10_000;
    this.fetchFn = options.fetch ?? fetch;
  }

  // ----- plumbing -----
  private async send(method: 'GET' | 'POST', path: string, body?: unknown): Promise<string> {
    const headers: Record<string, string> = { Accept: 'application/json' };
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (this.apiKey !== undefined) headers['Authorization'] = `Bearer ${this.apiKey}`;
    const controller = new AbortController();
    let timedOut = false;
    // An ordinary timer, cleared when the request ends. AbortSignal.timeout would not keep the process alive.
    const timer = setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, this.timeoutMs);
    let response: Response;
    let text: string;
    try {
      response = await this.fetchFn(this.base + path, {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      });
      text = await response.text();
    } catch (err) {
      const message = timedOut
        ? `the witness did not answer within ${this.timeoutMs} ms`
        : `could not reach the witness at ${this.base}: ${errorMessage(err)}`;
      throw new EvenhandError(message, 0, undefined, { cause: err });
    } finally {
      clearTimeout(timer);
    }
    if (!response.ok) {
      let parsed: unknown;
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = undefined;
      }
      const reason =
        typeof parsed === 'object' && parsed !== null && typeof (parsed as { error?: unknown }).error === 'string'
          ? (parsed as { error: string }).error
          : `HTTP ${response.status}`;
      throw new EvenhandError(reason, response.status, parsed ?? text);
    }
    return text;
  }

  private async json<T>(method: 'GET' | 'POST', path: string, body?: unknown): Promise<T> {
    const text = await this.send(method, path, body);
    try {
      return JSON.parse(text) as T;
    } catch (err) {
      throw new EvenhandError('the witness sent a reply that is not JSON', 0, text, { cause: err });
    }
  }

  private static id(value: string): string {
    return encodeURIComponent(value);
  }

  // ----- the witness -----
  /** Status, the witness's public key, and whether dev mode, the allow-list and saving are on. */
  health(): Promise<Health> {
    return this.json('GET', '/healthz');
  }

  /** Registers a mandate signed by the user's key. Registering the same mandate again is fine. */
  registerMandate(signedMandate: Record<string, unknown>): Promise<MandateRegistration> {
    return this.json('POST', '/v1/mandates', signedMandate);
  }

  /** Records one purchase and returns the witness's signed receipt. */
  record(mandateId: string, action: ActionInput): Promise<Receipt> {
    return this.json('POST', '/v1/receipts', { mandate_id: mandateId, action });
  }

  /** Judges one recorded action, or the latest when seq is left out. */
  dispute(mandateId: string, options: { seq?: number; revocations?: Revocation[] } = {}): Promise<Dispute> {
    const body: Record<string, unknown> = { mandate_id: mandateId };
    if (options.seq !== undefined) body['seq'] = options.seq;
    if (options.revocations !== undefined) body['revocations'] = options.revocations;
    return this.json('POST', '/v1/disputes', body);
  }

  disputeStatus(disputeId: string): Promise<Omit<Dispute, 'packet_url'>> {
    return this.json('GET', `/v1/disputes/${EvenhandClient.id(disputeId)}`);
  }

  /** The evidence packet for a dispute. */
  disputePacket(disputeId: string): Promise<Record<string, unknown>> {
    return this.json('GET', `/v1/disputes/${EvenhandClient.id(disputeId)}/packet`);
  }

  private static checkpoint(value: unknown): Checkpoint {
    try {
      parseCheckpoint(parseLossless(JSON.stringify(value)));
    } catch (err) {
      if (err instanceof SchemaError) throw new EvenhandError(`the witness sent a malformed checkpoint: ${err.message}`, 0, value, { cause: err });
      throw err;
    }
    return value as Checkpoint;
  }

  /**
   * Asks the witness to sign a checkpoint: "this chain holds N receipts, the last with this hash".
   * Save it somewhere the witness cannot reach. Asking again with nothing new returns the same one.
   */
  async checkpoint(mandateId: string): Promise<Checkpoint> {
    return EvenhandClient.checkpoint(await this.json('POST', '/v1/checkpoints', { mandate_id: mandateId }));
  }

  /** The latest checkpoint the witness has issued for a mandate. */
  async latestCheckpoint(mandateId: string): Promise<Checkpoint> {
    return EvenhandClient.checkpoint(await this.json('GET', `/v1/checkpoints/${EvenhandClient.id(mandateId)}`));
  }

  /** Exports a mandate's chain. Keep the text: it is what offline verification needs. */
  async exportChain(mandateId: string): Promise<ExportedChain> {
    const text = await this.send('GET', `/v1/chains/${EvenhandClient.id(mandateId)}`);
    try {
      return { text, bundle: JSON.parse(text) as Record<string, unknown> };
    } catch (err) {
      throw new EvenhandError('the witness sent a chain that is not JSON', 0, text, { cause: err });
    }
  }

  /** Asks the witness to check its own chain. This trusts the witness, so it proves little. Use verifyChain. */
  verifyOnServer(mandateId: string): Promise<ServerVerification> {
    return this.json('GET', `/v1/verify?mandate_id=${EvenhandClient.id(mandateId)}`);
  }

  /**
   * Exports a chain and checks it here, offline, against keys you pinned. The witness key is required,
   * because a chain checked only against keys it supplies proves nothing. Pass a checkpoint you saved
   * earlier to also catch receipts cut off the end, or history rewritten.
   */
  async verifyChain(
    mandateId: string,
    pinned: { witnessKey: string; userKey?: string; checkpoint?: Checkpoint | string },
  ): Promise<VerifyResult | CheckpointVerifyResult> {
    const { text } = await this.exportChain(mandateId);
    if (pinned.checkpoint !== undefined) {
      return verifyBundleWithCheckpoint(text, pinned.checkpoint, { witnessKey: pinned.witnessKey, userKey: pinned.userKey });
    }
    return verifyBundle(text, { witnessKey: pinned.witnessKey, userKey: pinned.userKey });
  }

  // ----- development helpers (the server must run with --dev) -----
  /** Creates, signs and registers a mandate with a throwaway user key. For testing only. */
  devMandate(input: DevMandateInput): Promise<DevMandate> {
    return this.json('POST', '/v1/dev/mandates', input);
  }

  /** Signs a revocation for a mandate made with devMandate in this run. For testing only. */
  devRevocation(mandateId: string, revokedAt?: number): Promise<{ revocation: Revocation; next: string }> {
    const body: Record<string, unknown> = { mandate_id: mandateId };
    if (revokedAt !== undefined) body['revoked_at'] = revokedAt;
    return this.json('POST', '/v1/dev/revocations', body);
  }

  // ----- wrapping an agent's purchase function -----
  /**
   * Wraps the function your agent calls to spend money. Every call that succeeds is recorded by the witness,
   * and the function's own result is returned unchanged. A call that throws records nothing.
   */
  tool<A extends unknown[], R>(
    mandateId: string,
    fn: (...args: A) => R | Promise<R>,
    options: ToolOptions<A> = {},
  ): (...args: A) => Promise<R> {
    const extract = options.extract ?? ((...args: A): ActionInput => defaultExtract(args));
    return async (...args: A): Promise<R> => {
      const action = extract(...args); // before the purchase, so a mistake here stops it
      const result = await fn(...args);
      try {
        await this.record(mandateId, action);
      } catch (err) {
        if (options.onWitnessError === 'ignore') return result;
        throw new WitnessError(`the purchase went through, but the witness could not record it: ${errorMessage(err)}`, result, { cause: err });
      }
      return result;
    };
  }
}
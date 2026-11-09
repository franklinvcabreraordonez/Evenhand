/** Shapes of what the witness server sends and accepts. Field names match the HTTP API exactly. */

export interface CartItem {
  sku: string;
  desc: string;
  price: number;
  qty: number;
}

export interface ActionRecord {
  mandate_id: string;
  agent_id: string;
  action: string;
  amount: number;
  currency: string;
  merchant_id: string;
  category: string;
  cart: CartItem[];
  step_up_done: boolean;
  ts: number;
}

export interface Receipt {
  seq: number;
  prev_hash: string;
  action: ActionRecord;
  witness_pub: string;
  signature: string;
  hash: string;
}

/** What you send to record a purchase. The witness stamps the time itself. */
export interface ActionInput {
  /** The merchant's id. */
  merchant: string;
  /** In major units of the mandate's currency, such as dollars. */
  amount: number;
  /** Describe the item, or send a cart. */
  description?: string;
  cart?: Array<{ sku?: string; desc: string; price: number; qty?: number }>;
  category?: string;
  sku?: string;
  currency?: string;
  agent_id?: string;
  action?: string;
  /** True when the user confirmed a large purchase. */
  step_up_done?: boolean;
}

export type VerdictType =
  | 'within_mandate'
  | 'exceeded_mandate'
  | 'ambiguous_intent'
  | 'evidence_insufficient'
  | 'evidence_invalid';

export interface VerdictCheck {
  name: string;
  passed: boolean;
  detail: string;
}

export interface Verdict {
  type: VerdictType;
  reasons: string[];
  recommendation: string;
  intent_score: number | null;
  checks: VerdictCheck[];
}

export interface Dispute {
  dispute_id: string;
  mandate_id: string;
  seq: number;
  status: 'decided' | 'needs_human_review' | 'needs_more_evidence';
  verdict: Verdict;
  packet_url?: string;
}

/** A user's signed revocation of a mandate, as the server sends and accepts it. */
export interface Revocation {
  mandate_id: string;
  revoked_at: number;
  principal_pub: string;
  signature: string;
}

export interface Checkpoint {
  format: 'evenhand-checkpoint-v1';
  mandate_id: string;
  /** How many receipts the chain held. */
  count: number;
  /** The hash of the last of them. */
  head: string;
  /** Whole seconds, the witness's clock. */
  issued_at: number;
  witness_pub: string;
  signature: string;
}

export interface Health {
  status: 'ok';
  witness_pub: string;
  version: string;
  dev_mode: boolean;
  principal_allowlist: boolean;
  persistent: boolean;
}

export interface MandateRegistration {
  mandate_id: string;
  digest: string;
  principal_pub: string;
  witness_pub: string;
}

export interface DevMandateInput {
  intent: string;
  mandate_id?: string;
  agent_id?: string;
  merchants?: string | string[];
  max_per_txn?: number;
  max_total?: number;
  categories?: string[];
  step_up_above?: number;
  hours?: number;
}

export interface DevMandate extends MandateRegistration {
  signed_mandate: Record<string, unknown>;
  warning: string;
}

export interface ServerVerification {
  ok: boolean;
  problems: string[];
  head: string | null;
  receipts: number;
}

/** An exported chain, with the exact text the server sent. Verify the text, not the parsed object. */
export interface ExportedChain {
  text: string;
  bundle: Record<string, unknown>;
}

export interface VerifyResult {
  ok: boolean;
  problems: string[];
  head: string | null;
  receipts: number;
  principalPub: string | null;
  witnessPub: string | null;
}

export interface CheckpointVerifyResult extends VerifyResult {
  checkpointCount: number | null;
  receiptsAfterCheckpoint: number | null;
}
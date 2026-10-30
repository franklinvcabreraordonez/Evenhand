# Synthetic dispute benchmark (n=2000, seed=7)

Ground truth comes from the generator, not from the adjudicator. Hard-constraint scenarios are
correct by construction; read the intent rows and the escalation numbers.

### Matcher: naive keyword coverage

- Overall agreement with ground truth: **78.8%** (1575/2000)
- Auto-resolved share: **74.5%**; accuracy on auto-resolved: **85.9%**
- Gray-zone recall (truly ambiguous cases correctly escalated): **54.6%**

| Scenario | n | Agreement |
|---|---|---|
| exceeded_cap | 134 | 100% |
| exceeded_cumulative | 121 | 100% |
| exceeded_merchant | 114 | 100% |
| expired | 75 | 100% |
| forgotten_within | 401 | 88% |
| intent_confusable | 229 | 7% |
| intent_qualifier_conflict | 204 | 96% |
| intent_vague | 155 | 0% |
| missing_receipt | 99 | 100% |
| missing_step_up | 69 | 100% |
| revoked | 87 | 100% |
| tampered | 207 | 100% |
| unrelated_purchase | 105 | 100% |

| truth \ predicted | within_mandate | exceeded_mandate | ambiguous_intent | evidence_insufficient | evidence_invalid |
|---|---|---|---|---|---|
| within_mandate | 353 | 13 | 35 | 0 | 0 |
| exceeded_mandate | 34 | 720 | 180 | 0 | 0 |
| ambiguous_intent | 0 | 163 | 196 | 0 | 0 |
| evidence_insufficient | 0 | 0 | 0 | 99 | 0 |
| evidence_invalid | 0 | 0 | 0 | 0 | 207 |

### Matcher: head-noun + abstain

- Overall agreement with ground truth: **94.2%** (1884/2000)
- Auto-resolved share: **77.5%**; accuracy on auto-resolved: **94.0%**
- Gray-zone recall (truly ambiguous cases correctly escalated): **91.1%**

| Scenario | n | Agreement |
|---|---|---|
| exceeded_cap | 134 | 100% |
| exceeded_cumulative | 121 | 100% |
| exceeded_merchant | 114 | 100% |
| expired | 75 | 100% |
| forgotten_within | 401 | 79% |
| intent_confusable | 229 | 100% |
| intent_qualifier_conflict | 204 | 84% |
| intent_vague | 155 | 100% |
| missing_receipt | 99 | 100% |
| missing_step_up | 69 | 100% |
| revoked | 87 | 100% |
| tampered | 207 | 100% |
| unrelated_purchase | 105 | 100% |

| truth \ predicted | within_mandate | exceeded_mandate | ambiguous_intent | evidence_insufficient | evidence_invalid |
|---|---|---|---|---|---|
| within_mandate | 317 | 61 | 23 | 0 | 0 |
| exceeded_mandate | 0 | 934 | 0 | 0 | 0 |
| ambiguous_intent | 0 | 32 | 327 | 0 | 0 |
| evidence_insufficient | 0 | 0 | 0 | 99 | 0 |
| evidence_invalid | 0 | 0 | 0 | 0 | 207 |

---
# Evidence packet case-0001

**Verdict:** `within_mandate`
**Recommendation:** Contest the dispute: the signed mandate authorized this action. Attach the evidence packet.

**Stated intent:** buy yoga mat under $60
**Mandate digest:** `e273f8ea726023f6...`  **Chain head:** `9bc554b09c2888ff...`

| Check | Result | Detail |
|---|---|---|
| mandate_signature | pass | verified |
| log_integrity | pass | chain intact |
| agent_binding | pass | action was performed by the authorized agent |
| action_type | pass | action 'purchase' vs allowed ['purchase'] |
| time_window | pass | within validity window |
| not_revoked | pass | mandate not revoked before action |
| per_txn_cap | pass | amount 47.06 vs cap 60.00 |
| cumulative_cap | pass | cumulative 64.38 vs cap 240.00 |
| merchant_allowed | pass | merchant gearlab |
| merchant_not_denied | pass | merchant gearlab |
| category_allowed | pass | category sports |
| intent_match | pass | score 1.00 |

**Disputed action (receipt 1):** purchase of 47.06 USD at gearlab (sports), items: yoga mat

---
# Evidence packet case-0010

**Verdict:** `exceeded_mandate`
**Recommendation:** Accept the dispute: the agent acted outside its mandate. Route liability per program terms.

**Stated intent:** buy wireless headphones under $200
**Mandate digest:** `4b5b54ad8dd37ea3...`  **Chain head:** `5f980e0ed143c57f...`

| Check | Result | Detail |
|---|---|---|
| mandate_signature | pass | verified |
| log_integrity | pass | chain intact |
| agent_binding | pass | action was performed by the authorized agent |
| action_type | pass | action 'purchase' vs allowed ['purchase'] |
| time_window | pass | within validity window |
| not_revoked | pass | mandate not revoked before action |
| per_txn_cap | FAIL | amount 256.59 vs cap 200.00 |
| cumulative_cap | pass | cumulative 360.28 vs cap 800.00 |
| merchant_allowed | pass | merchant bytebay |
| merchant_not_denied | pass | merchant bytebay |

**Disputed action (receipt 2):** purchase of 256.59 USD at bytebay (electronics), items: wireless headphones

---
# Evidence packet case-0015

**Verdict:** `evidence_invalid`
**Recommendation:** Treat as potential fraud or tampering. Escalate; do not rely on submitted logs.

**Stated intent:** buy ergonomic office chair under $510
**Mandate digest:** `93350db363f9d6cc...`  **Chain head:** `60b473c65e63907a...`

| Check | Result | Detail |
|---|---|---|
| mandate_signature | pass | verified |
| log_integrity | FAIL | receipt 0: signed by an unexpected witness key; receipt 0: signature does not verify (content altered or forged); receipt 1: signed by an unexpected witness key; receipt 1: signature does not verify (content altered or forged); receipt 2: signed by an unexpected witness key; receipt 2: signature does not verify (content altered or forged); receipt 3: signed by an unexpected witness key; receipt 3: signature does not verify (content altered or forged) |

**Disputed action (receipt 2):** purchase of 408.18 USD at seatly (office), items: ergonomic office chair

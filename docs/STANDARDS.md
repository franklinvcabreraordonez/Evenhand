# How Evenhand relates to Verifiable Intent and AP2

Checked on November 8, 2026. This is our reading of public documents, not an endorsement by any standards body, and not legal or compliance advice. Things here are changing fast, so check the sources at the bottom.

## What the two standards are

**Verifiable Intent (VI)** is an open specification, maintained by Mastercard and still a v0.1 draft. It defines a chain of signed credentials (SD-JWT) from a credential provider to a user to an agent, so a merchant or payment network can check that a purchase stayed within what the user delegated. It has two modes: the user confirms the exact purchase, or the user sets limits and the agent decides within them. It lists dispute resolution as out of scope.

**AP2 (Agent Payments Protocol)** was introduced by Google in September 2025. Version 0.2, released on April 28, 2026, expresses permission as signed checkout and payment mandates, each in an open form (limits) and a closed form (the final values), and uses the same mandate type names that VI uses.

**Who owns them now.** On April 28, 2026, Google and Mastercard contributed AP2 and VI to the FIDO Alliance, which handles them in its Payments Technical Working Group. FIDO's September 2026 timeline is to harmonize the two specifications by the end of 2026 and publish a first consolidated version in mid-2027.

## Where Evenhand fits

VI and AP2 record what the user authorized. Evenhand does something different: a witness outside the agent records what the agent actually did, each record is signed and chained to the one before, and a verdict compares the two when a purchase is disputed.

| | Verifiable Intent and AP2 | Evenhand |
|---|---|---|
| Records | What the user authorized | What the agent did, and a verdict comparing the two |
| Signed with | ES256 (ECDSA P-256) credentials | Ed25519 over canonical JSON |
| Money | Integer minor units, such as cents | Decimal amounts today |
| Disputes | Out of scope for VI | The point of the product |
| Who must adopt it | Wallets, networks, merchants and agent platforms | An agent's purchase function, or a gateway in front of it |

We expect them to be complementary. In a flow that uses VI, the authorization record can be held by the network. Evenhand's value there is independent evidence of execution and a neutral call in a dispute. For agents that do not use VI, which is most of them today, Evenhand supplies a simple signed mandate of its own.

## How VI's limits map onto an Evenhand mandate

| VI constraint | Evenhand mandate | Fit |
|---|---|---|
| Merchant allowlist (`mandate.checkout.allowed_merchants`) | `merchants_allow` | Close. VI identifies a merchant by name and website, Evenhand by an id |
| Payee allowlist (`mandate.payment.allowed_payees`) | `merchants_allow` | Close, with the same caveat |
| Amount range (`mandate.payment.amount_range`) | `max_per_txn` | Evenhand has no minimum |
| Budget (`mandate.payment.budget`) | `max_total` | Direct |
| Agent recurrence (`mandate.payment.agent_recurrence`) | The validity window | Partial. No occurrence count or schedule |
| Recurrence (`mandate.payment.recurrence`) | Nothing | Not supported |
| Line items by product and quantity (`mandate.checkout.line_items`) | Nothing | Evenhand uses a free-text intent instead |
| Checkout reference (`mandate.payment.reference`) | Nothing | Structural, not needed yet |
| Not in VI | Categories, human confirmation above an amount, merchant deny list, free-text intent | Evenhand only |

## Our position

We do not verify or issue VI or AP2 credentials today. Both are drafts that FIDO plans to harmonize, so an adapter built now would likely need rework. Honest verification of these credentials also means handling signed selective disclosure and key binding, which is security-critical code we would rather build once, against the consolidated specification.

When that specification is stable and a design partner needs it, the approach we expect to take is to verify the credential chain with the reference implementation, take the limits from the verified open mandate, record the credential's fingerprint in our receipts, and judge the executed action against both. Everything else in Evenhand, the witness, the chain and the checkpoints, does not depend on it.

## Sources

- Verifiable Intent specification and constraint definitions: https://github.com/agent-intent/verifiable-intent
- AP2 documentation, including the payment mandate schema: https://ap2-protocol.org/
- FIDO Alliance announcement, April 28, 2026: https://fidoalliance.org/fido-alliance-to-develop-standards-for-trusted-ai-agent-interactions/
- FIDO Payments working group timeline, W3C and GS1 workshop, September 2026: https://www.w3.org/2026/09/ecommerce-agents/slides/Jean_Luc_Di_Manno_FIDO.pdf
- Mastercard's description of Verifiable Intent in Agent Pay: https://developer.mastercard.com/mastercard-agent-pay/documentation/verifiable-intent/
EVENHAND WEBSITE: THE WORDS

HEAD
Title: Evenhand: a neutral witness for AI agents that spend money
Meta description: Evenhand is an open-source prototype that records what an AI agent did, checks it against what the user allowed, and returns a verdict with an evidence packet.

HEADER
Wordmark: Evenhand
Nav: How it works (#how) | Status (#status) | Talk to us (#talk) | GitHub (https://github.com/franklinvcabreraordonez/Evenhand)

HERO
Label: Open-source prototype
h1: A neutral witness for AI agents that spend money.
Intro: When an agent buys the wrong thing, nobody can prove what the user allowed. Evenhand records what the agent actually did, checks it against the user’s signed permission, and gives merchants, banks and insurers a verdict they can act on.
Buttons: See it run (https://github.com/franklinvcabreraordonez/Evenhand#quickstart) | Talk to us (#talk)

SECTION id="problem"
h2: Agent purchases have no neutral record.
Intro: As agents start to spend money for people, disputes get hard to settle, and every party’s evidence is partial.
Card: The log belongs to the agent | The record of what happened is often signed by the agent itself, so it proves little to anyone else.
Card: Merchants carry the loss | Merchants absorb the cost by default, and banks respond by restricting outside agents altogether.
Card: Nobody has said who pays | Card networks cover cards only and have not said who is responsible when an agent overreaches.

SECTION id="how"
h2: How Evenhand works
Intro: Three parts, each independently checkable.
Step 1: The user signs a mandate | What the agent may buy, from which merchants, for how much, and until when.
Step 2: A witness records the actions | Receipts are signed outside the agent’s control and chained together, so edits, deletions and reordering show up.
Step 3: Evenhand returns a verdict | Every check that ran is listed in an evidence packet a merchant, bank or insurer can verify offline.
Verdict labels: within_mandate, exceeded_mandate, ambiguous_intent, evidence_insufficient, evidence_invalid
Image: demo.png. Alt text: Evenhand browser demo: a receipt edited after the fact fails signature verification.
Caption: The browser demo that ships in the repository. Here a receipt was edited after signing, so its signature no longer verifies.

SECTION id="who"
h2: Who it is for
Intro: Evenhand is built for the people who have to settle these disputes.
Card: Teams that let agents spend | Agent wallets, card programs and shopping agents that need evidence a counterparty will accept.
Card: Merchants and issuers | Risk and dispute teams that need to tell a valid agent purchase from an unauthorized one.
Card: Insurers and auditors | Anyone pricing or reviewing agent risk who needs a verifiable record instead of a screenshot.

SECTION id="status"
h2: Where things stand
Paragraph 1: Version 0.1, an open-source prototype. It runs on synthetic data, has not been audited, and is not for real money or customer data.
Paragraph 2: The mandate format is a simplified one. Adapters for open agent-payment standards (Verifiable Intent and AP2) are planned and not yet built, and there is no hosted service yet.
Paragraph 3: The design commitments, including no custody of funds and no consumer scoring, are in the neutrality charter.
Links: Code on GitHub (https://github.com/franklinvcabreraordonez/Evenhand) | Evidence format (https://github.com/franklinvcabreraordonez/Evenhand/blob/main/docs/SPEC.md) | Neutrality charter (https://github.com/franklinvcabreraordonez/Evenhand/blob/main/docs/NEUTRALITY.md) | Synthetic benchmark (https://github.com/franklinvcabreraordonez/Evenhand/blob/main/docs/BENCHMARK.md)

SECTION id="talk"
h2: Talk to us
Paragraph: I am looking for 20-minute conversations with people who run agent payments, card programs, risk and disputes. No pitch. I want to learn what breaks today and what you would need to trust a neutral record. If you would consider being a design partner, say so.
Buttons: Message Franklin on LinkedIn (YOUR_LINKEDIN_URL, replace with your profile address) | Open a GitHub issue (https://github.com/franklinvcabreraordonez/Evenhand/issues/new?title=Conversation%20request)

FOOTER
Evenhand is an early-stage open-source project built by Franklin Cabrera Ordonez. Licensed under Apache-2.0. © 2026 Evenhand contributors.

HONESTY RULES
- Say "prototype" and "synthetic data". Do not say "platform", "enterprise-grade" or "trusted by".
- No customer logos, testimonials, pricing or "Inc." until each is true.
- Do not claim Verifiable Intent or AP2 support until the adapters exist.
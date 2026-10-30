# Evenhand

A neutral witness for AI agents that spend money.

> **Status: v0.1 prototype. Not production software. Tested only on synthetic data.**

## The problem

When an AI agent buys something or acts on an account and the outcome is disputed, nobody can prove what the user actually allowed. The record is often signed by the agent itself. Merchants absorb the loss by default, banks restrict outside agents, and payment networks cover cards only and have not said who pays when an agent overreaches.

## What Evenhand does

Evenhand records what an agent actually did from outside the agent's control (a **witness**), checks it against the user's signed authority (a **mandate**), and returns a **verdict** with an **evidence packet** that a merchant, bank or insurer can act on.

```
User signs MANDATE -> Agent acts -> WITNESS signs hash-chained RECEIPTS
Dispute opened -> ADJUDICATOR checks signatures, log integrity, limits, intent -> VERDICT + EVIDENCE PACKET
```

## Quickstart

Python 3.10 or newer (tested on 3.12).

```
python -m pip install -e .
python examples/quickstart.py
python -m unittest discover -s tests
```

The quickstart signs a mandate, records two agent purchases, issues verdicts, shows a tampered receipt being rejected, and prints an evidence packet.

## Verdicts

`within_mandate`, `exceeded_mandate`, `ambiguous_intent` (goes to a human), `evidence_insufficient`, `evidence_invalid` (tampered, forged or unverifiable).

## Design principles

1. **Evidence, not opinion.** Every verdict lists the checks behind it.
2. **The witness sits outside the agent's trust boundary.**
3. **The verifier is open and runs offline.**
4. **Interoperate with open standards** (Verifiable Intent and AP2, now under the FIDO Alliance) instead of inventing a new mandate format. Adapters are planned and **not yet built**.
5. **No custody of funds, no consumer scoring, humans decide adverse consumer outcomes.** See [docs/NEUTRALITY.md](docs/NEUTRALITY.md).

## What is in this repository

- `evenhand/`: the library (mandates, witness receipts, adjudicator, evidence packets, a synthetic dispute simulator).
- `examples/quickstart.py`: a runnable walkthrough.
- `docs/SPEC.md`: the evidence format as implemented. `docs/NEUTRALITY.md`: the neutrality charter. `docs/BENCHMARK.md`: the synthetic benchmark.
- `tests/`: unit tests for the signing, chain and adjudication logic.

## Limitations, stated plainly

- **The benchmark is synthetic.** Its ground truth comes from the generator in this repository, so scenarios that depend only on hard limits score 100% by construction. The informative result is where simple text matching fails (look-alike purchases, vague intents). It says nothing about real-world accuracy.
- Intent matching is a heuristic baseline; the model-based matcher is a stub.
- The mandate format is a simplified internal one. Verifiable Intent and AP2 adapters are planned.
- The witness is a library. A hosted witness with published checkpoints does not exist yet, and truncation of the end of a chain can only be detected against a checkpoint.
- Not audited. Do not use it for real money or real customer data.

## Roadmap

Verifiable Intent and AP2 adapters, a hosted witness with checkpoints, an MCP and HTTP gateway, non-card actions (ACH, wallets), and a human-review queue.

## Development and license

Parts of this code were drafted with AI assistance (Claude) under the maintainer's direction and are covered by tests. Contributions are welcome: see [CONTRIBUTING.md](CONTRIBUTING.md). Report security issues as described in [SECURITY.md](SECURITY.md).

Licensed under the Apache License 2.0. See [LICENSE](LICENSE).

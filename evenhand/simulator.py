"""Synthetic dispute generator with independent ground truth.

IMPORTANT: this world is far cleaner than reality. Hard-constraint scenarios are scored
correct by construction (the generator and the rules share semantics). The informative
numbers are the intent scenarios and the escalation rate. Real validity needs real
disputes labeled by human chargeback analysts.
"""
from __future__ import annotations

import dataclasses
import math
import random
import zlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .adjudicator import VerdictType
from .crypto import Signer
from .mandate import Constraints, Mandate, Revocation, SignedMandate, sign_mandate, sign_revocation
from .receipts import AgentAction, CartItem, Receipt, Witness

T0 = 1_800_000_000.0
HOUR, DAY = 3_600.0, 86_400.0

# product -> (bucket/category, (price low, price high), qualifier options)
PRODUCTS: Dict[str, Tuple[str, Tuple[int, int], List[str]]] = {
    "running shoes": ("sports", (60, 160), ["road", "trail"]),
    "yoga mat": ("sports", (15, 70), ["thick", "travel"]),
    "wireless headphones": ("electronics", (40, 350), ["noise cancelling", "sport"]),
    "laptop": ("electronics", (500, 1800), ["gaming", "ultrabook"]),
    "phone": ("electronics", (300, 1200), []),
    "coffee beans": ("grocery", (8, 30), ["organic", "decaf"]),
    "office chair": ("office", (90, 500), ["ergonomic", "mesh"]),
}
CONFUSABLE = {  # same bucket, different product: a purchase the principal did not ask for
    "running shoes": ["running socks"], "yoga mat": ["yoga strap"],
    "wireless headphones": ["headphone case"], "laptop": ["laptop bag", "laptop stand"],
    "phone": ["phone case"], "coffee beans": ["coffee mug"], "office chair": ["chair cushion"],
}
MERCHANTS = {
    "sports": ["stridemart", "athletix", "fieldhouse", "trailhead", "gearlab"],
    "electronics": ["voltshop", "circuitcity2", "gadgetbarn", "pixelhub", "bytebay"],
    "grocery": ["freshcart", "beanbox", "pantrypro", "greenbasket", "dailymart"],
    "office": ["deskdepot", "workwell", "ergostore", "officehaus", "seatly"],
}
VAGUE = [
    "a nice gift for my mom, around ${cap}",
    "stuff for my new apartment, keep it cheap, max ${cap}",
    "something I will actually use this month, up to ${cap}",
    "treat myself a little, budget ${cap}",
]

SYNONYM = {"running shoes": "sneakers", "laptop": "notebook computer", "phone": "smartphone",
           "wireless headphones": "earbuds", "yoga mat": "exercise mat", "office chair": "desk seat",
           "coffee beans": "ground coffee"}


def _messy(rng: random.Random, desc: str, product: str) -> str:
    """Real catalogs are noisy: typos, brands, bundles, synonyms. Truth label does not change."""
    kind = rng.choice(["typo", "brand", "bundle", "synonym"])
    if kind == "typo":
        words = desc.split()
        w = max(words, key=len)
        j = rng.randrange(1, len(w) - 1)
        words[words.index(w)] = w[:j] + w[j + 1:]
        return " ".join(words)
    if kind == "brand":
        return f"{rng.choice(['Nike', 'Acme', 'Apex', 'Zenith'])} {desc}"
    if kind == "bundle":
        return f"{desc} + {rng.choice(CONFUSABLE[product])}"
    return desc.replace(product, SYNONYM[product])

SCENARIOS = {  # name -> (weight, ground-truth label)
    "forgotten_within": (0.20, VerdictType.WITHIN_MANDATE),
    "exceeded_cap": (0.07, VerdictType.EXCEEDED_MANDATE),
    "exceeded_cumulative": (0.06, VerdictType.EXCEEDED_MANDATE),
    "exceeded_merchant": (0.06, VerdictType.EXCEEDED_MANDATE),
    "expired": (0.04, VerdictType.EXCEEDED_MANDATE),
    "revoked": (0.04, VerdictType.EXCEEDED_MANDATE),
    "missing_step_up": (0.04, VerdictType.EXCEEDED_MANDATE),
    "unrelated_purchase": (0.05, VerdictType.EXCEEDED_MANDATE),
    "intent_confusable": (0.12, VerdictType.EXCEEDED_MANDATE),
    "intent_qualifier_conflict": (0.10, VerdictType.AMBIGUOUS_INTENT),
    "intent_vague": (0.08, VerdictType.AMBIGUOUS_INTENT),
    "tampered": (0.09, VerdictType.EVIDENCE_INVALID),
    "missing_receipt": (0.05, VerdictType.EVIDENCE_INSUFFICIENT),
}


@dataclass
class Case:
    case_id: str
    scenario: str
    label: VerdictType
    signed_mandate: SignedMandate
    receipts: List[Receipt]
    target_seq: int
    witness_pub: str
    expected_principal_pub: str
    revocations: List[Revocation]


def _sku(desc: str) -> str:
    return f"sku-{zlib.crc32(desc.encode()) % 10**6}"


def _ctx(rng: random.Random, need_quals: bool = False) -> dict:
    names = [p for p, v in PRODUCTS.items() if v[2]] if need_quals else list(PRODUCTS)
    product = rng.choice(names)
    bucket, (lo, hi), quals = PRODUCTS[product]
    qual = rng.choice(quals) if quals and (need_quals or rng.random() < 0.6) else None
    price = round(rng.uniform(lo, hi), 2)
    cap = float(math.ceil(price * rng.uniform(1.15, 1.7) / 10) * 10)
    return dict(product=product, bucket=bucket, qual=qual, quals=quals, price=price, cap=cap,
                phrase=f"{qual} {product}" if qual else product)


def _action(m: Mandate, desc: str, amount: float, merchant: str, bucket: str, ts: float,
            step_up_done: bool = True, agent_id: Optional[str] = None) -> AgentAction:
    amount = round(amount, 2)
    return AgentAction(m.mandate_id, agent_id or m.agent_id, "purchase", amount, "USD", merchant, bucket,
                       (CartItem(_sku(desc), desc, amount, 1),), step_up_done, ts)


def build_case(rng: random.Random, i: int, scenario: str) -> Case:
    principal, witness = Signer(), Witness()
    ctx = _ctx(rng, need_quals=(scenario == "intent_qualifier_conflict"))
    bucket, cap, price, phrase = ctx["bucket"], ctx["cap"], ctx["price"], ctx["phrase"]
    pool = MERCHANTS[bucket]
    force_allow = scenario == "exceeded_merchant"
    allow = tuple(rng.sample(pool, 3)) if force_allow or rng.random() < 0.7 else None
    deny = ("sketchydeals",) if allow is None else ()
    merchant = rng.choice(allow) if allow else rng.choice(pool)

    step_up = cap * 0.8 if rng.random() < 0.4 else None
    if scenario == "missing_step_up":
        step_up = cap * 0.5
    cats = (bucket,) if (rng.random() < 0.7 or scenario == "unrelated_purchase") else None
    intent = f"buy {phrase} under ${int(cap)}"
    if scenario == "intent_vague":
        intent, cats = rng.choice(VAGUE).format(cap=int(cap)), None
    max_total_mult = 1.5 if scenario == "exceeded_cumulative" else 4.0

    m = Mandate(f"m{i}", f"principal-{i}", f"agent-{rng.randint(1, 5)}", T0, T0 + 30 * DAY, ("purchase",),
                Constraints(max_per_txn=cap, max_total=round(cap * max_total_mult, 2), merchants_allow=allow,
                            merchants_deny=deny, categories_allow=cats, step_up_above=step_up), intent)
    sm = sign_mandate(m, principal)
    ts = T0 + rng.uniform(3, 25) * DAY
    revocations: List[Revocation] = []

    # default: a valid purchase of the requested product
    desc, amount, act_bucket, step_done = phrase, price, bucket, True
    label = SCENARIOS[scenario][1]

    n_prior = rng.choice([0, 0, 1, 2])
    prior_amounts = [round(price * rng.uniform(0.2, 0.5), 2) for _ in range(n_prior)]

    if scenario == "exceeded_cap":
        amount = cap * rng.uniform(1.05, 1.5)
    elif scenario == "exceeded_cumulative":
        prior_amounts, amount = [round(cap * 0.9, 2)], cap * 0.8
    elif scenario == "exceeded_merchant":
        merchant = rng.choice(["sketchydeals", "shadymart"])
    elif scenario == "expired":
        ts = m.expires_at + rng.uniform(HOUR, 3 * DAY)
    elif scenario == "revoked":
        revocations.append(sign_revocation(m.mandate_id, ts - rng.uniform(HOUR, 2 * DAY), principal))
    elif scenario == "missing_step_up":
        amount, step_done = cap * rng.uniform(0.6, 0.95), False
    elif scenario == "unrelated_purchase":
        other = rng.choice([p for p, v in PRODUCTS.items() if v[0] != bucket])
        act_bucket, desc = PRODUCTS[other][0], other
        merchant, amount = rng.choice(MERCHANTS[act_bucket]), min(cap, rng.uniform(*PRODUCTS[other][1]))
    elif scenario == "intent_confusable":
        desc, amount = rng.choice(CONFUSABLE[ctx["product"]]), rng.uniform(5, 40)
    elif scenario == "intent_qualifier_conflict":
        desc = f"{rng.choice([q for q in ctx['quals'] if q != ctx['qual']])} {ctx['product']}"
    elif scenario == "tampered":
        prior_amounts = [round(price * rng.uniform(0.2, 0.5), 2) for _ in range(2)]

    if scenario in ("forgotten_within", "intent_qualifier_conflict") and rng.random() < 0.35:
        desc = _messy(rng, desc, ctx["product"])

    for k, pa in enumerate(prior_amounts):
        witness.record(_action(m, phrase, pa, merchant if scenario != "exceeded_merchant" else rng.choice(allow),
                               bucket, T0 + (k + 1) * rng.uniform(1, 5) * HOUR))
    target = witness.record(_action(m, desc, amount, merchant, act_bucket, ts, step_done))
    if scenario == "tampered":
        witness.record(_action(m, phrase, price * 0.3, rng.choice(allow) if allow else rng.choice(pool), bucket, ts + HOUR))

    receipts = witness.chain(m.mandate_id)
    witness_pub, target_seq = witness.public_hex, target.seq

    if scenario == "tampered":
        kind = rng.choice(["amount", "delete", "reorder", "forged_mandate", "forged_witness"])
        if kind == "amount":
            receipts[target_seq] = dataclasses.replace(
                target, action=dataclasses.replace(target.action, amount=round(target.action.amount * 0.5, 2)))
        elif kind == "delete":
            del receipts[0]
        elif kind == "reorder":
            receipts[0], receipts[1] = receipts[1], receipts[0]
        elif kind == "forged_mandate":
            sm = sign_mandate(m, Signer())
        elif kind == "forged_witness":
            fake = Witness()
            for r in witness.chain(m.mandate_id):
                fake.record(r.action)
            receipts = fake.chain(m.mandate_id)
    elif scenario == "missing_receipt":
        receipts = receipts[:-1]  # target omitted; remaining chain still verifies

    return Case(f"case-{i:04d}", scenario, label, sm, receipts, target_seq, witness_pub,
                principal.public_hex, revocations)


def generate(n: int, seed: int = 7) -> List[Case]:
    rng = random.Random(seed)
    names = list(SCENARIOS)
    weights = [SCENARIOS[s][0] for s in names]
    return [build_case(rng, i, rng.choices(names, weights)[0]) for i in range(n)]

"""Run the synthetic dispute benchmark: python -m scripts.run_sim --n 1000 --seed 7"""
from __future__ import annotations

import argparse
import collections
import os

from evenhand.adjudicator import AUTO_RESOLVED, VerdictType, adjudicate
from evenhand.evidence import build_packet, render_markdown
from evenhand.intent import HeadNounIntentMatcher, KeywordIntentMatcher
from evenhand.simulator import generate

ORDER = [VerdictType.WITHIN_MANDATE, VerdictType.EXCEEDED_MANDATE, VerdictType.AMBIGUOUS_INTENT,
         VerdictType.EVIDENCE_INSUFFICIENT, VerdictType.EVIDENCE_INVALID]


def run(cases, matcher):
    out = []
    for c in cases:
        v = adjudicate(c.signed_mandate, c.receipts, c.target_seq, c.witness_pub,
                       expected_principal_pub=c.expected_principal_pub, revocations=c.revocations,
                       intent_matcher=matcher)
        out.append((c, v))
    return out


def summarize(name, results):
    n = len(results)
    correct = sum(v.type == c.label for c, v in results)
    by = collections.defaultdict(lambda: [0, 0])
    conf = collections.Counter()
    for c, v in results:
        by[c.scenario][1] += 1
        by[c.scenario][0] += v.type == c.label
        conf[(c.label, v.type)] += 1
    auto = [(c, v) for c, v in results if v.type in AUTO_RESOLVED]
    auto_ok = sum(v.type == c.label for c, v in auto)
    gt_amb = [(c, v) for c, v in results if c.label == VerdictType.AMBIGUOUS_INTENT]
    amb_recall = sum(v.type == VerdictType.AMBIGUOUS_INTENT for c, v in gt_amb) / max(1, len(gt_amb))
    lines = [f"### Matcher: {name}", "",
             f"- Overall agreement with ground truth: **{correct / n:.1%}** ({correct}/{n})",
             f"- Auto-resolved share: **{len(auto) / n:.1%}**; accuracy on auto-resolved: **{auto_ok / max(1, len(auto)):.1%}**",
             f"- Gray-zone recall (truly ambiguous cases correctly escalated): **{amb_recall:.1%}**", "",
             "| Scenario | n | Agreement |", "|---|---|---|"]
    for s, (ok, tot) in sorted(by.items()):
        lines.append(f"| {s} | {tot} | {ok / tot:.0%} |")
    lines += ["", "| truth \\ predicted | " + " | ".join(o.value for o in ORDER) + " |", "|---|" + "---|" * len(ORDER)]
    for t in ORDER:
        lines.append(f"| {t.value} | " + " | ".join(str(conf[(t, p)]) for p in ORDER) + " |")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    cases = generate(args.n, args.seed)
    report = [f"# Synthetic dispute benchmark (n={args.n}, seed={args.seed})", "",
              "Ground truth comes from the generator, not from the adjudicator. Hard-constraint scenarios are",
              "correct by construction; read the intent rows and the escalation numbers.", ""]
    res_head = None
    for name, matcher in [("naive keyword coverage", KeywordIntentMatcher()),
                          ("head-noun + abstain", HeadNounIntentMatcher())]:
        res = run(cases, matcher)
        report += [summarize(name, res), ""]
        if name.startswith("head"):
            res_head = res
    # sample packets
    shown = set()
    for c, v in res_head:
        if c.scenario in ("forgotten_within", "exceeded_cap", "tampered") and c.scenario not in shown:
            shown.add(c.scenario)
            packet = build_packet(c.case_id, c.signed_mandate, c.receipts, c.target_seq, v, c.witness_pub)
            report += ["---", render_markdown(packet), ""]
    text = "\n".join(report)
    os.makedirs("docs", exist_ok=True)
    with open("docs/BENCHMARK.md", "w") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    main()

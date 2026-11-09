"""Writes test fixtures for the TypeScript tests, using the Python code as the source of truth.

    python make_fixtures.py canonical OUT.json    floats, ints, strings and objects, with Python's canonical text
    python make_fixtures.py bundles OUT.json      exported chains and many damaged copies, with Python's verdict on each

Run it from the repository root, so that the evenhand package can be imported.
"""
import copy
import json
import math
import random
import struct
import sys
import time

sys.path.insert(0, ".")

from evenhand import Gateway, quick_mandate, verify_bundle
from evenhand.checkpoint import checkpoint_to_dict, make_checkpoint, verify_bundle_with_checkpoint
from evenhand.crypto import canonical

SKIPPED = []


def random_double(rng):
    while True:
        x = struct.unpack("<d", struct.pack("<Q", rng.getrandbits(64)))[0]
        if math.isfinite(x):
            return x


def random_object(rng, depth):
    def leaf():
        c = rng.randint(0, 6)
        if c == 0:
            return None
        if c == 1:
            return rng.random() < 0.5
        if c == 2:
            return rng.randint(-1000, 10 ** 12)
        if c == 3:
            return round(rng.uniform(-1000, 1000), rng.randint(0, 6))
        if c == 4:
            return random_double(rng)
        return "".join(rng.choice("ab \u00e9\u4e2d\U0001f600\n\"") for _ in range(rng.randint(0, 6)))

    if depth == 0:
        return leaf()
    if rng.random() < 0.5:
        return [random_object(rng, depth - 1) for _ in range(rng.randint(0, 4))]
    return {"".join(rng.choice("abc\u00e9\U0001f600") for _ in range(rng.randint(1, 3))): random_object(rng, depth - 1)
            for _ in range(rng.randint(0, 4))}


def canonical_cases(rng):
    floats = [0.0, -0.0, 1e16, 9999999999999998.0, 1e15, 123456789012345.0, 1e-5, 0.0001, 0.00012345, 1e22, 1e21, 1.5e300,
              1.7976931348623157e308, 5e-324, 2.2250738585072014e-308, 0.1, 0.30000000000000004, 99.0, 12345.5, 1e100,
              1234567890123456.0, 12345678901234567.0, 0.5, 100.0, 2.5e-7, 4.35, 0.07, 1731024000.123456]
    floats += [random_double(rng) for _ in range(3000)]
    floats += [rng.uniform(0, 1000) for _ in range(500)] + [round(rng.uniform(0, 500), 2) for _ in range(500)]
    floats += [float(rng.randint(1, 10 ** rng.randint(1, 20))) for _ in range(300)]
    floats += [rng.random() * 10 ** rng.randint(-12, 25) for _ in range(500)]
    ints = [0, 1, -1, 99, 12345678901234567890, -10 ** 30, 2 ** 63, 2 ** 53 + 1]
    ints += [rng.randint(-10 ** 18, 10 ** 18) for _ in range(200)]
    alphabet = ["a", "Z", " ", "~", "\x7f", "\x00", "\x1f", "\n", "\t", "\"", "\\", "/", "\u00e9", "\u00fc", "\u4e2d", "\u5e97",
                "\u2028", "\ud7ff", "\ue000", "\uffff", "\U0001f600", "\U00010000", "\U0010ffff", "$", "'"]
    strings = ["", "plain", "stridemart", "caf\u00e9", "\U0001f600 emoji", "line\nbreak", "tab\tand \"quotes\" and \\slashes\\"]
    strings += ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 12))) for _ in range(800)]
    cases = []
    for x in floats:
        cases.append({"kind": "float", "text": repr(x), "canonical": canonical(x).decode()})
    for n in ints:
        cases.append({"kind": "int", "text": str(n), "canonical": canonical(n).decode()})
    for s in strings:
        cases.append({"kind": "string", "text": json.dumps(s, ensure_ascii=True), "canonical": canonical(s).decode()})
    # key orders that differ between code points and UTF-16 units
    for keys in (["a", "b"], ["\uffff", "\U00010000"], ["\ue000", "\U0001f600", "z"], ["B", "a", "_", "\u00e9"], ["x", "", "xx"]):
        d = {k: i for i, k in enumerate(keys)}
        cases.append({"kind": "object", "text": json.dumps(d), "canonical": canonical(d).decode()})
    for _ in range(200):
        d = random_object(rng, 3)
        cases.append({"kind": "object", "text": json.dumps(d), "canonical": canonical(d).decode()})
    return cases


MERCHANTS = ["stridemart", "caf\u00e9-bleu", "\u5e97\u5b50", "shop \U0001f600", "a\"b", "x\\y"]
DESCRIPTIONS = ["road running shoes", "caf\u00e9 au lait", "tab\there", "line\nbreak", "\x01control\x7f", "\U0001f600 fun", "", "$5.99 \"deal\""]
AMOUNTS = [10, 30, 99, 12.5, 30.0, 0.1, 1e-7, 1.5e16, 123456.789, 7, 0, 119.99, 4.35]


def make_chain(rng, n_receipts, mandate_id):
    use_ints = rng.random() < 0.4
    signed, principal = quick_mandate(
        intent="buy running shoes under $120", merchants=MERCHANTS if rng.random() < 0.5 else None,
        max_per_txn=120 if use_ints else 120.5, max_total=None if rng.random() < 0.5 else 1000, categories=["sports"],
        mandate_id=mandate_id, step_up_above=None if rng.random() < 0.5 else 500,
        now=time.time() if rng.random() < 0.5 else 1731024000.5)
    gw = Gateway(signed, expected_principal_pub=principal.public_hex)
    for _ in range(n_receipts):
        gw.record(merchant=rng.choice(MERCHANTS), amount=rng.choice(AMOUNTS), description=rng.choice(DESCRIPTIONS), category="sports")
    return gw, principal


def leaf_paths(obj, path=()):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from leaf_paths(v, path + (k,))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from leaf_paths(v, path + (i,))
    else:
        yield path


def get_at(obj, path):
    for p in path:
        obj = obj[p]
    return obj


def set_at(obj, path, value):
    for p in path[:-1]:
        obj = obj[p]
    obj[path[-1]] = value


def mutate_leaf(rng, bundle):
    out = copy.deepcopy(bundle)
    paths = [p for p in leaf_paths(out) if p and p[0] in ("mandate", "receipts", "witness_pub", "head")]
    path = rng.choice(paths)
    old = get_at(out, path)
    if isinstance(old, bool):
        new = not old
    elif isinstance(old, int):
        new = rng.choice([old + 1, float(old), str(old), old - 1, None])
    elif isinstance(old, float):
        whole = int(old) if old == int(old) and abs(old) < 1e15 else old * 2
        new = rng.choice([old + 0.5, whole, str(old), -old, None, old * (1 + 1e-12)])
    elif isinstance(old, str):
        new = rng.choice([old + "x", old[:-1], old.upper() if old.upper() != old else old + "!", "", 5, None,
                          old[::-1] if len(old) > 1 and old[::-1] != old else old + "?"])
    else:
        new = rng.choice([1, "x", True, [], {}])
    set_at(out, path, new)
    return out, "leaf " + "/".join(str(p) for p in path)


def structural_mutations(bundle, other):
    muts = []
    r = bundle["receipts"]
    if len(r) >= 1:
        muts.append(("drop last receipt", {**bundle, "receipts": r[:-1], "head": r[-2]["hash"] if len(r) > 1 else None}))
        muts.append(("drop last receipt, keep stated head", {**bundle, "receipts": r[:-1]}))
        muts.append(("drop first receipt", {**bundle, "receipts": r[1:]}))
        muts.append(("duplicate a receipt", {**bundle, "receipts": r + [r[-1]]}))
    if len(r) >= 2:
        muts.append(("swap two receipts", {**bundle, "receipts": [r[1], r[0]] + r[2:]}))
        muts.append(("drop a middle receipt", {**bundle, "receipts": r[:1] + r[2:]}))
    if other["receipts"]:
        muts.append(("receipt from another chain", {**bundle, "receipts": r[:-1] + [other["receipts"][0]]}))
        muts.append(("mandate from another chain", {**bundle, "mandate": other["mandate"]}))
    muts.append(("no receipts key", {k: v for k, v in bundle.items() if k != "receipts"}))
    muts.append(("no revocations key", {k: v for k, v in bundle.items() if k != "revocations"}))
    muts.append(("no hash on receipts", {**bundle, "receipts": [{k: v for k, v in x.items() if k != "hash"} for x in r]}))
    muts.append(("wrong format", {**bundle, "format": "evenhand-bundle-v2"}))
    muts.append(("extra keys", {**bundle, "extra": 1, "mandate": {**bundle["mandate"], "note": "x"}}))
    muts.append(("not an object", [bundle]))
    return muts


def case_for(name, text, witness_key, user_key, checkpoint):
    try:
        parsed = json.loads(text)
        result = verify_bundle(parsed, expected_principal_pub=user_key, expected_witness_pub=witness_key)
        py = {"ok": result["ok"], "head": result["head"], "receipts": result["receipts"],
              "malformed": bool(result["problems"]) and result["problems"][0].startswith("malformed")}
        cp_out = None
        if checkpoint is not None:
            cpr = verify_bundle_with_checkpoint(parsed, checkpoint, expected_witness_pub=witness_key, expected_principal_pub=user_key)
            cp_out = {"ok": cpr["ok"], "count": cpr["checkpoint_count"], "after": cpr["receipts_after_checkpoint"]}
    except Exception as exc:  # Python itself cannot judge this one; report it so a Python bug is not hidden
        SKIPPED.append(f"{name}: {type(exc).__name__}: {exc}")
        return None
    return {"name": name, "text": text, "witness_key": witness_key, "user_key": user_key, "checkpoint": checkpoint,
            "python": py, "python_checkpoint": cp_out}


def bundle_cases(rng):
    cases = []
    chains = []
    for i in range(14):
        gw, principal = make_chain(rng, rng.randint(1, 6), f"m-fix-{i}")
        bundle = json.loads(json.dumps(gw.export()))
        upto = rng.randint(1, len(gw.receipts))
        cp = checkpoint_to_dict(make_checkpoint(gw.witness.signer, f"m-fix-{i}", gw.receipts[:upto], 1731024000))
        chains.append((bundle, gw.witness_pub, principal.public_hex, cp))
    for i, (bundle, wk, uk, cp) in enumerate(chains):
        nxt = chains[(i + 1) % len(chains)]
        for text in (json.dumps(bundle), json.dumps(bundle, indent=2), json.dumps(bundle, separators=(",", ":")),
                     json.dumps(bundle, ensure_ascii=False)):
            cases.append(case_for(f"valid {i}", text, wk, uk, cp))
        cases.append(case_for(f"valid {i} wrong witness key", json.dumps(bundle), nxt[1], uk, cp))
        cases.append(case_for(f"valid {i} wrong user key", json.dumps(bundle), wk, nxt[2], cp))
        for name, mutated in structural_mutations(bundle, nxt[0]):
            cases.append(case_for(f"{i}: {name}", json.dumps(mutated), wk, uk, cp))
        for _ in range(60):
            mutated, name = mutate_leaf(rng, bundle)
            cases.append(case_for(f"{i}: {name}", json.dumps(mutated), wk, uk, cp))
        # the same numbers written differently must not verify, in either language
        text = json.dumps(bundle)
        for old, new in ((".0,", ","), (".0}", "}")):
            if old in text:
                cases.append(case_for(f"{i}: number text changed {old!r}", text.replace(old, new, 1), wk, uk, cp))
        cases.append(case_for(f"{i}: checkpoint from another chain", json.dumps(bundle), wk, uk, nxt[3]))
        cases.append(case_for(f"{i}: checkpoint count changed", json.dumps(bundle), wk, uk, dict(cp, count=cp["count"] + 1)))
    return [c for c in cases if c is not None]


def main():
    mode, out = sys.argv[1], sys.argv[2]
    rng = random.Random(20261108)
    data = canonical_cases(rng) if mode == "canonical" else bundle_cases(rng)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(data, f)
    print(f"wrote {len(data)} cases to {out}" + (f"; python could not judge {len(SKIPPED)}" if SKIPPED else ""))
    for line in SKIPPED[:5]:
        print("  skipped:", line, file=sys.stderr)


if __name__ == "__main__":
    main()
"""Bootstrap/Holm decision for the H2 search gates (stream-performance, GSP05 first).

Usage: python decide_sp_search_gate.py protocol.json results.jsonl decision.json

Rule (protocol["decision_rule"]):
- per run and scenario the harness reports cold/warm steady latency p50/p95 (ms);
  baseline arm = rule["baseline"], candidate = rule["candidate"]; n = all reps of each arm;
- improvement: for each (scenario, kind, stat) in rule["improve"], H0 "candidate - baseline >= 0"
  rejected by Holm over that family at alpha (one-sided bootstrap, seeded);
- regression: for each (scenario, kind, stat) in rule["guard"], H0 "candidate - baseline <= 0"
  rejected by Holm over that family at alpha;
- hard checks: identical source digests per (scenario, kind, index) for rule["identical_sources"]
  scenarios across every run of both arms; breaker never opens in rule["never_open"] scenarios
  (candidate); no failed run.
"""

import json
import random
import sys
from pathlib import Path


def bootstrap(a, b, rng, n):
    observed = sum(b) / len(b) - sum(a) / len(a)
    ge = le = 0
    for _ in range(n):
        sa = [a[rng.randrange(len(a))] for _ in a]
        sb = [b[rng.randrange(len(b))] for _ in b]
        diff = sum(sb) / len(sb) - sum(sa) / len(sa)
        ge += diff >= 0
        le += diff <= 0
    return observed, (ge + 1) / (n + 1), (le + 1) / (n + 1)


def holm(pvalues: dict, alpha: float) -> set:
    rejected = set()
    ordered = sorted(pvalues.items(), key=lambda item: item[1])
    for rank, (key, p) in enumerate(ordered):
        if p <= alpha / (len(ordered) - rank):
            rejected.add(key)
        else:
            break
    return rejected


def main() -> None:
    protocol = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    records = [json.loads(line) for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines() if line.strip()]
    rule = protocol["decision_rule"]
    rng = random.Random(rule.get("seed", 20261110))
    alpha, resamples = rule.get("alpha", 0.05), rule.get("resamples", 10000)
    base, cand = rule["baseline"], rule["candidate"]
    ok = [r for r in records if not r.get("failed")]

    def runs(arm: str, scenario: str) -> list[dict]:
        return [run for r in ok if r["arm"] == arm for run in r["result"]["runs"] if run["scenario"] == scenario]

    def values(arm: str, scenario: str, kind: str, stat: str) -> list[float]:
        return [float(run[kind]["steady_latency_ms"][stat]) for run in runs(arm, scenario)
                if run[kind]["steady_latency_ms"].get(stat) is not None]

    tests = {}
    for family in ("improve", "guard"):
        for scenario, kind, stat in rule[family]:
            a, b = values(base, scenario, kind, stat), values(cand, scenario, kind, stat)
            diff, p_ge, p_le = bootstrap(a, b, rng, resamples)
            tests[(family, scenario, kind, stat)] = {"mean_baseline": round(sum(a) / len(a), 1),
                                                     "mean_candidate": round(sum(b) / len(b), 1),
                                                     "diff_ms": round(diff, 1), "n": [len(a), len(b)],
                                                     "p_improvement_null": p_ge, "p_regression_null": p_le}
    improved = holm({k: t["p_improvement_null"] for k, t in tests.items() if k[0] == "improve"}, alpha)
    regressions = holm({k: t["p_regression_null"] for k, t in tests.items() if k[0] == "guard"}, alpha)

    digest_mismatches = []
    for scenario in rule.get("identical_sources", []):
        reference: dict = {}
        for r in ok:
            for run in r["result"]["runs"]:
                if run["scenario"] != scenario:
                    continue
                for row in run.get("requests", []):
                    key = (row["kind"], row["index"])
                    digest = row.get("source_digest")
                    if key in reference and reference[key] != digest:
                        digest_mismatches.append(f"{scenario} {key} {r['arm']} rep {r['rep']}")
                    reference.setdefault(key, digest)
    never_open_violations = [f"{scenario} rep {r['rep']}" for scenario in rule.get("never_open", [])
                             for r in ok if r["arm"] == cand for run in r["result"]["runs"]
                             if run["scenario"] == scenario and isinstance(run.get("breaker"), dict)
                             and run["breaker"].get("openings")]
    failed = [f"{r['arm']} rep {r['rep']}" for r in records if r.get("failed")]
    improve_family = [k for k in tests if k[0] == "improve"]
    passed = (len(improved) == len(improve_family) and not regressions and not digest_mismatches
              and not never_open_violations and not failed)
    out = {"protocol": protocol["id"], "decision": "PASS" if passed else "FAIL",
           "improved": sorted(" ".join(k[1:]) for k in improved),
           "regressions": sorted(" ".join(k[1:]) for k in regressions),
           "digest_mismatches": digest_mismatches[:20], "never_open_violations": never_open_violations,
           "failed_runs": failed, "tests": {" ".join(k): t for k, t in tests.items()}}
    Path(sys.argv[3]).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({k: out[k] for k in ("decision", "improved", "regressions", "failed_runs")}
                     | {"digest_mismatches": len(digest_mismatches), "never_open_violations": len(never_open_violations)}))


if __name__ == "__main__":
    main()

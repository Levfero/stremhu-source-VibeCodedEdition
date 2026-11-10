"""Bootstrap/Holm decision for the H2 search gates (stream-performance: GSP05, GSP06).

Usage: python decide_sp_search_gate.py protocol.json results.jsonl decision.json

Rule (protocol["decision_rule"]):
- per run and scenario the harness reports per-kind (cold/warm) distributions; a test entry is
  [scenario, kind, stat] (metric "steady_latency_ms", lower is better) or
  [scenario, kind, stat, metric, "lower"|"higher"]; with plan variants the scenario is
  "<variant>:<scenario>" (e.g. "t10:S1");
- baseline arm = rule["baseline"], candidate = rule["candidate"]; n = all reps of each arm;
- improvement: for each entry in rule["improve"], H0 "candidate is not better than baseline"
  rejected by Holm over that family at alpha (one-sided bootstrap of the mean difference, seeded);
- regression: for each entry in rule["guard"], H0 "candidate is not worse than baseline by more
  than rule["guard_margin"][metric] (default 0)" rejected by Holm over that family at alpha;
- hard checks: identical source digests (rule["identical_sources"]) and rendered-stream digests
  (rule["identical_enrichment"]) per (scenario, kind, index) across every run of both arms; breaker
  never opens in rule["never_open"] scenarios (candidate); candidate minus baseline mean Tech Info
  calls per cold request within rule["max_extra_tech_info_calls_per_cold_request"]["limit"] for its
  scenarios; no failed run.
"""

import json
import random
import sys
from pathlib import Path


def bootstrap(a, b, rng, n):
    """Observed mean difference (b - a) and the resampled differences (seeded, fixed draw order)."""
    observed = sum(b) / len(b) - sum(a) / len(a)
    diffs = []
    for _ in range(n):
        sa = [a[rng.randrange(len(a))] for _ in a]
        sb = [b[rng.randrange(len(b))] for _ in b]
        diffs.append(sum(sb) / len(sb) - sum(sa) / len(sa))
    return observed, diffs


def p_at_least(diffs, threshold):
    return (sum(d >= threshold for d in diffs) + 1) / (len(diffs) + 1)


def p_at_most(diffs, threshold):
    return (sum(d <= threshold for d in diffs) + 1) / (len(diffs) + 1)


def holm(pvalues: dict, alpha: float) -> set:
    rejected = set()
    ordered = sorted(pvalues.items(), key=lambda item: item[1])
    for rank, (key, p) in enumerate(ordered):
        if p <= alpha / (len(ordered) - rank):
            rejected.add(key)
        else:
            break
    return rejected


def scenario_key(record: dict, run: dict) -> str:
    variant = record.get("variant")
    return f"{variant}:{run['scenario']}" if variant else run["scenario"]


def main() -> None:
    protocol = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    records = [json.loads(line) for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines() if line.strip()]
    rule = protocol["decision_rule"]
    rng = random.Random(rule.get("seed", 20261110))
    alpha, resamples = rule.get("alpha", 0.05), rule.get("resamples", 10000)
    base, cand = rule["baseline"], rule["candidate"]
    ok = [r for r in records if not r.get("failed")]

    def runs(arm: str, scenario: str) -> list[dict]:
        return [run for r in ok if r["arm"] == arm for run in r["result"]["runs"] if scenario_key(r, run) == scenario]

    def values(arm: str, scenario: str, kind: str, stat: str, metric: str) -> list[float]:
        return [float(run[kind][metric][stat]) for run in runs(arm, scenario)
                if run[kind][metric].get(stat) is not None]

    tests = {}
    for family in ("improve", "guard"):
        for entry in rule[family]:
            scenario, kind, stat = entry[:3]
            metric = entry[3] if len(entry) > 3 else "steady_latency_ms"
            direction = entry[4] if len(entry) > 4 else "lower"
            margin = float(rule.get("guard_margin", {}).get(metric, 0.0))
            a, b = values(base, scenario, kind, stat, metric), values(cand, scenario, kind, stat, metric)
            diff, diffs = bootstrap(a, b, rng, resamples)
            # Lower is better: improvement rejects "diff >= 0"; regression rejects "diff <= margin"
            # (a worsening within the registered margin is not a regression). Mirrored for higher.
            if direction == "lower":
                p_improve, p_regress = p_at_least(diffs, 0.0), p_at_most(diffs, margin)
            else:
                p_improve, p_regress = p_at_most(diffs, 0.0), p_at_least(diffs, -margin)
            key = (family, scenario, kind, stat) + ((metric,) if metric != "steady_latency_ms" else ())
            tests[key] = {"metric": metric, "better": direction, "guard_margin": margin,
                          "mean_baseline": round(sum(a) / len(a), 3), "mean_candidate": round(sum(b) / len(b), 3),
                          "diff": round(diff, 3), "n": [len(a), len(b)],
                          "p_improvement_null": p_improve, "p_regression_null": p_regress}
    improved = holm({k: t["p_improvement_null"] for k, t in tests.items() if k[0] == "improve"}, alpha)
    regressions = holm({k: t["p_regression_null"] for k, t in tests.items() if k[0] == "guard"}, alpha)

    def mismatches(scenarios: list[str], field: str) -> list[str]:
        found = []
        for scenario in scenarios:
            reference: dict = {}
            for r in ok:
                for run in r["result"]["runs"]:
                    if scenario_key(r, run) != scenario:
                        continue
                    for row in run.get("requests", []):
                        key = (row["kind"], row["index"])
                        digest = row.get(field)
                        if key in reference and reference[key] != digest:
                            found.append(f"{scenario} {key} {r['arm']} rep {r['rep']}")
                        reference.setdefault(key, digest)
        return found

    digest_mismatches = mismatches(rule.get("identical_sources", []), "source_digest")
    enrichment_mismatches = mismatches(rule.get("identical_enrichment", []), "enrichment_digest")
    never_open_violations = [f"{scenario_key(r, run)} rep {r['rep']}" for scenario in rule.get("never_open", [])
                             for r in ok if r["arm"] == cand for run in r["result"]["runs"]
                             if scenario_key(r, run) == scenario and isinstance(run.get("breaker"), dict)
                             and run["breaker"].get("openings")]
    call_checks = {}
    call_rule = rule.get("max_extra_tech_info_calls_per_cold_request")
    if call_rule:
        for scenario in call_rule["scenarios"]:
            per_arm = {arm: [run["tech_info"]["calls_per_cold_request"] for run in runs(arm, scenario)]
                       for arm in (base, cand)}
            extra = sum(per_arm[cand]) / len(per_arm[cand]) - sum(per_arm[base]) / len(per_arm[base])
            call_checks[scenario] = {"extra_calls_per_cold_request": round(extra, 3),
                                     "within_limit": extra <= call_rule["limit"]}
    call_violations = [s for s, c in call_checks.items() if not c["within_limit"]]
    failed = [f"{r['arm']} rep {r['rep']}" + (f" {r['variant']}" if r.get("variant") else "")
              for r in records if r.get("failed")]
    improve_family = [k for k in tests if k[0] == "improve"]
    passed = (len(improved) == len(improve_family) and not regressions and not digest_mismatches
              and not enrichment_mismatches and not never_open_violations and not call_violations and not failed)
    out = {"protocol": protocol["id"], "decision": "PASS" if passed else "FAIL",
           "improved": sorted(" ".join(k[1:]) for k in improved),
           "regressions": sorted(" ".join(k[1:]) for k in regressions),
           "digest_mismatches": digest_mismatches[:20], "enrichment_mismatches": enrichment_mismatches[:20],
           "never_open_violations": never_open_violations, "tech_info_call_checks": call_checks,
           "failed_runs": failed, "tests": {" ".join(k): t for k, t in tests.items()}}
    Path(sys.argv[3]).write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({k: out[k] for k in ("decision", "improved", "regressions", "failed_runs")}
                     | {"digest_mismatches": len(digest_mismatches),
                        "enrichment_mismatches": len(enrichment_mismatches),
                        "never_open_violations": len(never_open_violations), "call_violations": call_violations}))


if __name__ == "__main__":
    main()

"""Apply a registered stream-performance gate rule (before/after series) to a summary.

Usage: python decide_sp_gate.py protocol.json summary.json baseline_summary.json complete.json decision.json

Rule shape (protocol["decision_rule"]): primary metric, key cells, improvement/regression
by median difference beyond the noise J, where J per cell and metric is
max(|median C0 series0 - median C0 series1|, baseline B/B2 J of the same profile/piece at
30 Mbit/s, 0.05 s). Labels: C0 = before (legacy), C1 = after (candidate).
"""

import json
import sys
from pathlib import Path

GUARD_METRICS = ["start.time_to_start_s", "start.ttfb_s", "seek.resume_s", "start.stall_s"]


def median(summary_cell: dict, label: str, metric: str):
    return summary_cell.get(label, {}).get(metric, {}).get("median")


def main() -> None:
    protocol = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    summary = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    baseline = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
    rule = protocol["decision_rule"]
    primary = rule["primary_metric"].split(" ")[0]
    cells = summary["cells"]
    cell_names = sorted({key.split("/series")[0] for key in cells})

    def noise(cell: str, metric: str) -> float:
        s0, s1 = cells.get(f"{cell}/series0", {}), cells.get(f"{cell}/series1", {})
        m0, m1 = median(s0, "C0", metric), median(s1, "C0", metric)
        own = abs(m0 - m1) if m0 is not None and m1 is not None else 0.0
        profile, piece, _ = cell.split("/")
        pb0 = baseline["cells"].get(f"{profile}/{piece}/30Mbit", {}).get("noise_J", {}).get(metric, 0.0)
        return round(max(own, pb0, 0.05), 3)

    per_cell = {}
    for cell in cell_names:
        entry = {}
        for series in (0, 1):
            data = cells.get(f"{cell}/series{series}", {})
            result = {}
            for metric in GUARD_METRICS:
                c0, c1 = median(data, "C0", metric), median(data, "C1", metric)
                if c0 is None or c1 is None:
                    result[metric] = None
                    continue
                j = noise(cell, metric)
                result[metric] = {"c0": c0, "c1": c1, "delta_c0_minus_c1": round(c0 - c1, 3), "J": j,
                                  "improved": c0 - c1 > j, "regressed": c1 - c0 > j}
            entry[f"series{series}"] = result
        per_cell[cell] = entry

    key_improved = [cell for cell in rule["key_cells"]
                    if all((per_cell.get(cell, {}).get(f"series{s}", {}).get(primary) or {}).get("improved")
                           for s in (0, 1))]
    regressions = [(cell, series, metric) for cell, entry in per_cell.items() for series, result in entry.items()
                   for metric, value in result.items() if value and value["regressed"]]
    records = json.loads(Path(sys.argv[4]).read_text(encoding="utf-8"))["records"]
    failed = {label: sum(1 for r in records if r["label"] == label and r.get("failed")) for label in ("C0", "C1")}
    tracebacks = {label: sum(r.get("app_log", {}).get("tracebacks", 0) for r in records if r["label"] == label)
                  for label in ("C0", "C1")}
    checks = {
        "improved_key_cells_both_series": key_improved,
        "improved_count": len(key_improved),
        "improved_required": 4,
        "regressions": regressions,
        "failed_runs": failed,
        "tracebacks_total": tracebacks,
    }
    passed = (len(key_improved) >= 4 and not regressions and failed["C1"] <= failed["C0"]
              and tracebacks["C1"] <= tracebacks["C0"])
    decision = {"protocol": protocol["id"], "decision": "PASS" if passed else "FAIL", "checks": checks,
                "per_cell": per_cell}
    Path(sys.argv[5]).write_text(json.dumps(decision, indent=1) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"decision": decision["decision"], "improved": len(key_improved),
                      "regressions": len(regressions), "failed": failed}))


if __name__ == "__main__":
    main()

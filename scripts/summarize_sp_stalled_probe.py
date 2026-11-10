"""G0 / SP10: fold probe_sp_stalled_peers.py batch outputs into one evidence JSON.

Usage (host, no Docker):
  python scripts/summarize_sp_stalled_probe.py --image-id <docker image id> \\
      --out docs/feature/stream-performance/evidence/g0-sp10-stalled-probe-v2.json \\
      [--interpretation interpretation.json] U1.json U2.json ...

Per (scenario, swarm, piece size, variant): median/min/max of the piece-0 time, of the time
until pieces 0-2 are all done and of the last piece; how long after its first request a
stalled peer was snubbed; serving-peer snubs (false positives of a shorter timeout).
The probe is a functional G0 check, so the output is marked is_measurement: false.
"""

import argparse
import datetime
import hashlib
import json
import statistics
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
FIRST = ("0", "1", "2")


def sha256_lf(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def stats(values) -> dict | None:
    values = [v for v in values if v is not None]
    if not values:
        return None
    return {"median": round(statistics.median(values), 2), "min": round(min(values), 2),
            "max": round(max(values), 2), "n": len(values)}


def cell(runs: list[dict]) -> dict:
    first3 = [max(r["piece_s"][k] for k in FIRST) if all(r["piece_s"].get(k) is not None for k in FIRST) else None
              for r in runs]
    snub_after = [p["snub_s"] - p["first_request_s"] for r in runs for p in r["stalled_peers"]
                  if p.get("snub_s") is not None and p.get("first_request_s") is not None]
    out = {
        "n": len(runs),
        "piece0_s": stats([r["piece_s"]["0"] for r in runs]),
        "first3_s": stats(first3),
        "last_piece_s": stats([r["piece_s"].get("last") for r in runs]),
        "not_done": sum(1 for r in runs if not r["all_done"]),
        "piece0_stuck_runs": (sum(1 for r in runs if r["stuck_blocks"]["0"]["max"])
                              if runs[0]["stuck_blocks"] else None),
        "stalled_snub_after_first_request_s": stats(snub_after),
        "serving_snubs_total": sum(len(r.get("serving_snubs_s", [])) for r in runs),
        "serving_block_timeouts_total": sum(r["block_timeouts_serving_peers"] for r in runs),
        "redundant_mib_median": round(statistics.median(r["redundant_mib"] for r in runs), 2),
    }
    if runs[0].get("watchdog_blocks") is not None:
        blocks = [b for r in runs for b in r["watchdog_blocks"]]
        out["watchdog_blocks"] = len(blocks)
        out["watchdog_false_blocks"] = sum(1 for b in blocks if not b["was_stalled"])
        out["watchdog_block_at_s"] = stats([b["at_s"] for b in blocks])
    return out


def compact(run: dict) -> dict:
    row = {"v": run["variant"], "mib": run["piece_mib"], "rep": run["rep"], "piece_s": run["piece_s"],
           "redundant_mib": run["redundant_mib"]}
    if run["stalled_peers"]:
        row["stalled"] = [{k: p.get(k) for k in ("first_request_s", "max_queue", "snub_s", "first_block_timeout_s",
                                                  "block_timeouts")} for p in run["stalled_peers"]]
    if run["stuck_blocks"]:
        row["stuck_piece0"] = run["stuck_blocks"]["0"]
    if run.get("serving_snubs_s"):
        row["serving_snubs_s"] = run["serving_snubs_s"]
    if run["watchdog_blocks"] is not None:
        row["watchdog_blocks"] = run["watchdog_blocks"]
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("batches", nargs="+", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--image", default="stremhu-source:sp-cand-c3d6035")
    parser.add_argument("--interpretation", type=Path)
    args = parser.parse_args()
    batches, rows, runs, variants, native = [], {}, {}, {}, None
    for path in args.batches:
        doc = json.loads(path.read_text(encoding="utf-8"))
        cfg = doc["config"]
        native = doc["native_version"]
        batches.append({"name": path.stem, "scenario": cfg["scenario"], "swarm": cfg["swarm"],
                        "piece_mib": cfg["piece_mib"], "reps": cfg["reps"], "variants": list(cfg["variants"]),
                        "wall_s": doc["wall_s"], "runs": len(doc["runs"]), "seed_stats": doc["seed_stats"]})
        variants.update(cfg["variants"])
        key = f"{cfg['scenario']}/{cfg['swarm']}"
        for run in doc["runs"]:
            rows.setdefault((key, run["piece_mib"], run["variant"]), []).append(run)
            runs.setdefault(key, []).append(compact(run))
    summary: dict = {}
    for (key, piece, variant), group in sorted(rows.items()):
        summary.setdefault(key, {}).setdefault(f"{piece}MiB", {})[variant] = cell(group)
    script = REPO / "scripts/probe_sp_stalled_peers.py"
    evidence = {
        "id": args.out.stem, "item": "SP10",
        "kind": "G0 functional probe (raw libtorrent, synthetic swarm); not a gate measurement, selects no default",
        "is_measurement": False,
        "recorded_at_utc": datetime.datetime.now(datetime.UTC).isoformat(),
        "provenance": {
            "script": "scripts/probe_sp_stalled_peers.py", "script_sha256_lf": sha256_lf(script),
            "helpers_sha256_lf": {p: sha256_lf(REPO / p) for p in (
                "scripts/sp_seed_service.py", "scripts/fixtures/local_bt_seed.py",
                "scripts/fixtures/synthetic_torrent.py", "scripts/summarize_sp_stalled_probe.py")},
            "image": args.image, "image_id": args.image_id, "native_version": native,
        },
        "variants": variants, "batches": batches, "summary": summary,
        "interpretation": (json.loads(args.interpretation.read_text(encoding="utf-8"))
                           if args.interpretation else "TODO: stable facts vs n-small hints"),
        "runs": runs,
    }
    args.out.write_text(json.dumps(evidence, indent=1, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"{args.out} ({args.out.stat().st_size} bytes, {sum(b['runs'] for b in batches)} runs)")


if __name__ == "__main__":
    main()

"""Run an H2 search gate (stream-performance) sequentially in disposable containers.

Usage: python run_sp_search_gate.py --plan plan.json --out results.jsonl --workdir <dir>

plan.json: {"image": "...", "arms": {"C0": "legacy", "C5": "candidate"}, "series": 2, "reps_per_series": 5,
            "harness_args": [...], "cpuset": "0,1", "cpus": 2, "memory": "1g"}

Every run: a fresh `--network none` container of the image under test (product code at /app),
the harness mounted read-only, one policy, its JSON written to the work dir; arms rotate per
repetition. The harness blocks every external HTTP attempt and uses synthetic trackers only.
"""

import argparse
import json
import subprocess
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workdir", required=True)
    args = parser.parse_args()
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    workdir = Path(args.workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    arms = list(plan["arms"])
    image_id = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", plan["image"]],
                              capture_output=True, text=True, check=True).stdout.strip()
    order = 0
    for series in range(plan["series"]):
        for offset in range(plan["reps_per_series"]):
            rep = series * plan["reps_per_series"] + offset
            rotation = arms[rep % len(arms):] + arms[: rep % len(arms)]
            for arm in rotation:
                name = f"{rep:02d}-{arm}.json"
                started = time.monotonic()
                proc = subprocess.run(
                    ["docker", "run", "--rm", "--network", "none", "--cpuset-cpus", plan.get("cpuset", "0,1"),
                     "--cpus", str(plan.get("cpus", 2)), "--memory", plan.get("memory", "1g"),
                     "-v", f"{SCRIPTS}:/sp/scripts:ro", "-v", f"{workdir}:/sp/out", "-w", "/app",
                     "-e", "PYTHONPATH=/app", "--entrypoint", "python", plan["image"],
                     "/sp/scripts/benchmark_sp_search.py", "--policies", plan["arms"][arm],
                     *plan["harness_args"], "--out", f"/sp/out/{name}"],
                    capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=7200)
                record = {"order": order, "series": series, "rep": rep, "arm": arm, "policy": plan["arms"][arm],
                          "image_id": image_id, "exit": proc.returncode, "wall_s": round(time.monotonic() - started, 1)}
                result_path = workdir / name
                if proc.returncode == 0 and result_path.exists():
                    record["result"] = json.loads(result_path.read_text(encoding="utf-8"))
                else:
                    record["failed"] = (proc.stderr or "")[-400:]
                with Path(args.out).open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
                print(json.dumps({"order": order, "arm": arm, "rep": rep, "exit": proc.returncode,
                                  "wall_s": record["wall_s"]}), flush=True)
                order += 1


if __name__ == "__main__":
    main()

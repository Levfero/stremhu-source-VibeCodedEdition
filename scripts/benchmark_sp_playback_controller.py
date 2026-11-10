"""H1 controller (stream-performance): runs playback sessions on local Docker, one cell at a time.

Runs on the host. Every run gets its own internal (no internet) Docker network,
a fresh data volume and fresh containers:

- seed:   the measurement-tool image, ``sp_seed_service.py`` (synthetic swarm + local tracker)
- app:    the image under test (A/B/C), normal start, HOST_IP = its address on the internal network
- client: the measurement-tool image, ``benchmark_sp_playback.py``

The product finds the seed through its normal announce path. Raw product logs are
never stored (they may contain request URLs); only traceback/error line counts are.

Optional, GSP04 "fully downloaded" mode (absent = the run below, unchanged):
  "payload": "block-index"   synthetic content with a per-block pattern (byte-exact checks)
  "downloaded": true         after the fixture: a warm-up read of the whole file through the
                             product (benchmark_sp_bulk_read.py, not a sample; incomplete or not
                             byte-exact = failed run), then the seeds are stopped, so every later
                             byte comes from the product's own disk: record["warm"]
  "downloaded_settle_s": 2   pause after the seeds are stopped
  "bulk_read_args": [...]    then one measured bulk read (benchmark_sp_bulk_read.py with these
                             arguments, straight to the app, own stats window and app cgroup CPU
                             time) before the player emulator: record["result"]["bulk"]

Usage:
  python benchmark_sp_playback_controller.py --plan plan.json --out results.jsonl [--only N]

plan.json: {"tool_image": "...", "images": {"A": "...", "B": "..."}, "reps": 5,
            "cells": [{"profile": "mixed", "piece_size": 4194304, "bitrate_mbit": 30}],
            "profiles": {"mixed": {"peers": 8, "profiles": [...]}}, "total_size": 1073741824,
            "app_env": {"KEY": "value"}  # optional, per image key: {"C1": {...}}
            }
"""

import argparse
import json
import re
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
SUBNET_BASE = "172.29.{}.0/24"


def sh(*args, check=True, capture=True, timeout=600) -> str:
    proc = subprocess.run(args, check=False, capture_output=capture, text=True, timeout=timeout, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"{args[:3]} exit {proc.returncode}: {(proc.stderr or '')[-400:]}")
    return proc.stdout if capture else ""


def win_path(path: Path) -> str:
    return str(path.resolve())


class StatsSampler(threading.Thread):
    def __init__(self, container: str):
        super().__init__(daemon=True)
        self.container = container
        self.samples: list[tuple[float, float]] = []
        self.halt = threading.Event()

    def run(self):
        while not self.halt.is_set():
            out = sh("docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}|{{.MemUsage}}", self.container,
                     check=False, timeout=30).strip()
            m = re.match(r"([\d.]+)%\|([\d.]+)(MiB|GiB|KiB)", out)
            if m:
                mem = float(m.group(2)) * {"KiB": 1 / 1024, "MiB": 1, "GiB": 1024}[m.group(3)]
                self.samples.append((float(m.group(1)), mem))

    def summary(self) -> dict:
        if not self.samples:
            return {"samples": 0}
        cpu = sorted(s[0] for s in self.samples)
        mem = sorted(s[1] for s in self.samples)
        return {"samples": len(self.samples), "cpu_pct_mean": round(sum(cpu) / len(cpu), 1),
                "cpu_pct_p95": cpu[int(len(cpu) * 0.95) - 1 if len(cpu) > 1 else 0], "mem_mib_max": round(mem[-1], 1)}


def wait_ready(app: str, address: str, deadline_s: float) -> float:
    started = time.monotonic()
    probe = ("import ssl,urllib.request,sys;c=ssl.create_default_context();c.check_hostname=False;"
             "c.verify_mode=ssl.CERT_NONE;"
             f"r=urllib.request.urlopen('https://{address}:7070/api/health',context=c,timeout=3);"
             "sys.exit(0 if r.status==200 else 1)")
    while time.monotonic() - started < deadline_s:
        if subprocess.run(["docker", "exec", app, "python", "-c", probe], check=False, capture_output=True).returncode == 0:
            return time.monotonic() - started
        time.sleep(1)
    raise TimeoutError("app not healthy before deadline")


def app_cpu_usec(app: str) -> int | None:
    """CPU time of the app container's cgroup (cgroup v2 cpu.stat), None when unavailable."""
    out = sh("docker", "exec", app, "cat", "/sys/fs/cgroup/cpu.stat", check=False, timeout=30)
    match = re.search(r"^usage_usec (\d+)$", out, re.MULTILINE)
    return int(match.group(1)) if match else None


def bulk_read(plan: dict, net: str, client_ip: str, mounts: list[str], app_ip: str, shared: Path, name: str,
              args: list) -> dict:
    """GSP04: one benchmark_sp_bulk_read.py run straight to the app; returns its JSON result."""
    payload = ["--payload", plan["payload"]] if plan.get("payload") else []
    sh("docker", "run", "--rm", "--network", net, "--ip", client_ip, "--cpuset-cpus", "4,5", *mounts,
       "--entrypoint", "python", plan["tool_image"], "/sp/scripts/benchmark_sp_bulk_read.py",
       "--targets", "/sp/shared/targets.json", "--target-id", "t", "--base", f"https://{app_ip}:7070",
       "--out", f"/sp/shared/{name}.json", *payload, *[str(a) for a in args], timeout=900)
    return json.loads((shared / f"{name}.json").read_text(encoding="utf-8"))


def run_one(plan: dict, label: str, image: str, cell: dict, rep: int, order: int, workdir: Path) -> dict:
    run_id = f"{label}-{cell['profile']}-{cell['piece_size'] >> 20}m-{rep}-{uuid.uuid4().hex[:6]}"
    shared = workdir / run_id
    shared.mkdir(parents=True)
    octet = 40 + (order % 200)
    subnet = SUBNET_BASE.format(octet)
    app_ip, client_ip = f"172.29.{octet}.20", f"172.29.{octet}.30"
    proxy_ip = f"172.29.{octet}.40"
    proxy = f"sp-proxy-{run_id}" if plan.get("latency_proxy_one_way_ms") else None
    net, vol = f"sp-net-{run_id}", f"sp-data-{run_id}"
    app = f"sp-app-{run_id}"
    profile = plan["profiles"][cell["profile"]]
    peer_profiles = [profile["profiles"][i % len(profile["profiles"])] for i in range(profile["peers"])]
    peer_ips = [f"172.29.{octet}.{100 + i}" for i in range(len(peer_profiles))]
    seeds = [f"sp-seed{i}-{run_id}" for i in range(len(peer_profiles))]
    torrent = {"id": "t", "name": f"sp-{cell['piece_size'] >> 10}k.mkv", "piece_size": cell["piece_size"],
               "total_size": plan["total_size"]}
    if plan.get("payload"):
        torrent["payload"] = plan["payload"]
    for i, peer_profile in enumerate(peer_profiles):
        # One peer per container (own address); peer 0 also runs the tracker advertising all of them.
        spec = {"peers": 1, "seed": rep * 100 + i, "host": "0.0.0.0", "port_base": 6900,
                "tracker": {"port": 6969, "advertise": peer_ips[0], "peers": [[ip, 6900] for ip in peer_ips]}
                if i == 0 else None,
                "profiles": [peer_profile], "torrents": [torrent]}
        (shared / f"spec{i}.json").write_text(json.dumps(spec), encoding="utf-8")
    mounts = ["-v", f"{win_path(SCRIPTS)}:/sp/scripts:ro", "-v", f"{win_path(shared)}:/sp/shared"]
    record: dict = {"run_id": run_id, "label": label, "image": image, "image_id": plan.get("image_ids", {}).get(label),
                    "cell": cell, "rep": rep, "order": order}
    sampler = None
    try:
        sh("docker", "network", "create", "--internal", "--subnet", subnet, net)
        sh("docker", "volume", "create", vol)
        for i, (name, ip) in enumerate(zip(seeds, peer_ips, strict=True)):
            extra = ["--torrent-dir", "/sp/shared/torrents"] if i == 0 else []
            sh("docker", "run", "-d", "--name", name, "--network", net, "--ip", ip, "--cpuset-cpus", "2,3",
               "--memory", "256m", *mounts, "--entrypoint", "python", plan["tool_image"],
               "/sp/scripts/sp_seed_service.py", "--spec", f"/sp/shared/spec{i}.json", *extra,
               "--stats", f"/sp/shared/seed-stats{i}.json")
        for name in seeds:
            for _ in range(60):
                if '"ready": true' in sh("docker", "logs", name, check=False):
                    break
                time.sleep(0.5)
            else:
                raise TimeoutError("seed not ready")
        env = []
        for key, value in plan.get("app_env", {}).get(label, {}).items():
            env += ["-e", f"{key}={value}"]
        sh("docker", "run", "-d", "--name", app, "--network", net, "--ip", app_ip, "--cpuset-cpus", "0,1",
           "--cpus", "2", "--memory", "1g", "--memory-swap", "1g", "-e", f"HOST_IP={app_ip}", *env,
           "-v", f"{vol}:/app/data", *mounts, image)
        record["ready_s"] = round(wait_ready(app, app_ip, 180), 2)
        fixture = sh("docker", "exec", "-w", "/app", "-e", "PYTHONPATH=/app", app, "python",
                     "/sp/scripts/sp_app_fixture.py", "--torrent-dir", "/sp/shared/torrents",
                     "--out", "/sp/shared/targets.json")
        record["fixture"] = json.loads(fixture.strip().splitlines()[-1])
        if plan.get("downloaded"):
            # GSP04: the whole file through the product first (not a sample), then no peer is left.
            warm = bulk_read(plan, net, client_ip, mounts, app_ip, shared, "warm", ["--mark-bytes", 1 << 20])
            record["warm"] = warm
            if not warm.get("complete") or warm.get("payload_errors") or warm.get("status") != 206:
                raise RuntimeError("warm-up read not complete and byte-exact")
            sh("docker", "stop", "-t", "15", *seeds, check=False)
            time.sleep(plan.get("downloaded_settle_s", 2))
            if plan.get("bulk_read_args") is not None:
                bulk_sampler = StatsSampler(app)
                cpu_before = app_cpu_usec(app)
                bulk_sampler.start()
                try:
                    bulk = bulk_read(plan, net, client_ip, mounts, app_ip, shared, "bulk", plan["bulk_read_args"])
                finally:
                    bulk_sampler.halt.set()
                    bulk_sampler.join(timeout=40)
                cpu_after = app_cpu_usec(app)
                if cpu_before is not None and cpu_after is not None:
                    bulk["app_cpu_s"] = round((cpu_after - cpu_before) / 1e6, 3)
                    if bulk["bytes_received"]:
                        bulk["app_cpu_s_per_gib"] = round(bulk["app_cpu_s"] / (bulk["bytes_received"] / (1 << 30)), 3)
                bulk["app_stats"] = bulk_sampler.summary()
                record["bulk"] = bulk
        base_ip = app_ip
        if proxy:
            # H3: fixed one-way latency between player and product (both directions).
            sh("docker", "run", "-d", "--name", proxy, "--network", net, "--ip", proxy_ip, "--cpuset-cpus", "6,7",
               *mounts, "--entrypoint", "python", plan["tool_image"], "/sp/scripts/fixtures/latency_proxy.py",
               "--listen", "0.0.0.0:7070", "--target", f"{app_ip}:7070",
               "--one-way-ms", str(plan["latency_proxy_one_way_ms"]))
            for _ in range(60):
                if '"ready": true' in sh("docker", "logs", proxy, check=False):
                    break
                time.sleep(0.5)
            else:
                raise TimeoutError("latency proxy not ready")
            base_ip = proxy_ip
            record["latency_proxy_one_way_ms"] = plan["latency_proxy_one_way_ms"]
        sampler = StatsSampler(app)
        sampler.start()
        client = sh("docker", "run", "--rm", "--network", net, "--ip", client_ip, "--cpuset-cpus", "4,5", *mounts,
                    "--entrypoint", "python", plan["tool_image"], "/sp/scripts/benchmark_sp_playback.py",
                    "--targets", "/sp/shared/targets.json", "--target-id", "t", "--base", f"https://{base_ip}:7070",
                    "--out", "/sp/shared/result.json", "--bitrate-mbit", str(cell["bitrate_mbit"]),
                    *[str(a) for a in plan.get("client_args", [])], timeout=900)
        record["client_line"] = json.loads(client.strip().splitlines()[-1])
        sampler.halt.set()
        sh("docker", "stop", "-t", "15", *seeds, check=False)
        record["result"] = json.loads((shared / "result.json").read_text(encoding="utf-8"))
        if "bulk" in record:
            record["result"]["bulk"] = record.pop("bulk")  # GSP04: the decider reads result.*
        per_peer = []
        for i in range(len(seeds)):
            path = shared / f"seed-stats{i}.json"
            per_peer.append(json.loads(path.read_text(encoding="utf-8")) if path.exists() else None)
        handshakes = [v for s in per_peer if s for v in s.get("first_handshake_monotonic", {}).values()]
        handshake = min(handshakes) if handshakes else None
        seed_stats = {
            "peers_with_stats": sum(1 for s in per_peer if s),
            "peers_connected": sum(1 for s in per_peer if s and s["handshakes"]),
            "bytes_sent": sum(s["bytes_sent"] for s in per_peer if s),
            "bytes_sent_per_peer": [s["bytes_sent"] if s else None for s in per_peer],
            "requests_ignored": sum(s["requests_ignored"] for s in per_peer if s),
            "cancels": sum(s["cancels"] for s in per_peer if s),
            "tracker_announces": per_peer[0]["tracker_announces"] if per_peer and per_peer[0] else None,
            "errors": [e for s in per_peer if s for e in s["errors"]],
        }
        record["seed"] = seed_stats
        first_byte = record["result"].get("first_byte_monotonic")
        if handshake is not None and first_byte is not None:
            record["ttfb_after_first_handshake_s"] = round(first_byte - handshake, 3)
            record["first_handshake_after_start_s"] = round(handshake - record["result"]["t_start_monotonic"], 3)
        received = record["result"]["start"]["bytes_received"] + record["result"].get("seek", {}).get("bytes_received", 0)
        if seed_stats["peers_with_stats"] == len(seeds):
            record["unused_downloaded_bytes"] = seed_stats["bytes_sent"] - received
        else:
            record["seed_stats_incomplete"] = True
        logs = subprocess.run(["docker", "logs", app], check=False, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=60)
        text = (logs.stdout or "") + (logs.stderr or "")
        lines = text.splitlines()
        starts = [i for i, line in enumerate(lines) if "Traceback" in line]
        # A traceback "with an application frame" mentions the product's code (/app/app/...) before the
        # next traceback starts (or within 80 lines); asyncio-internal ones (gsp02-v3 finding) do not.
        app_tracebacks = sum(
            1 for k, start in enumerate(starts)
            if any("/app/app/" in line for line in lines[start:min(starts[k + 1] if k + 1 < len(starts) else len(lines),
                                                                    start + 80)]))
        record["app_log"] = {"tracebacks": len(starts), "app_tracebacks": app_tracebacks,
                             "error_lines": sum(1 for line in lines if "ERROR" in line)}
        if plan.get("capture_log_prefix"):
            # Diagnostics only: lines with the opt-in prefix (relay-level fields, no URLs; masked anyway).
            prefix = plan["capture_log_prefix"]
            kept = [re.sub(r"(https?://\S+|/api/\S+)", "<masked-url>", line)[:400]
                    for line in text.splitlines() if prefix in line]
            (shared / "diag.log").write_text("\n".join(kept), encoding="utf-8")
        if plan.get("capture_tracebacks") and "Traceback" in text:
            # Diagnostics only: traceback blocks with every URL path/query masked (they may carry keys).
            blocks, keep = [], False
            for line in text.splitlines():
                if "Traceback" in line:
                    keep = True
                    blocks.append("----")
                if keep:
                    blocks.append(re.sub(r"(https?://\S+|/api/\S+)", "<masked-url>", line)[:300])
                    if line and not line.startswith((" ", "Traceback", "\t")) and "Error" in line:
                        keep = False
            (shared / "tracebacks.txt").write_text("\n".join(blocks), encoding="utf-8")
    except Exception as error:
        record["failed"] = f"{type(error).__name__}: {str(error)[:300]}"
    finally:
        if sampler is not None:
            sampler.halt.set()
            sampler.join(timeout=40)
            record["app_stats"] = sampler.summary()
        sh("docker", "rm", "-f", app, *seeds, *([proxy] if proxy else []), check=False)
        sh("docker", "volume", "rm", "-f", vol, check=False)
        sh("docker", "network", "rm", net, check=False)
        for name in ("targets.json",):
            (shared / name).unlink(missing_ok=True)
    return record


# Failures of Docker itself (daemon or CLI connection), never of the measured image: a `docker run`
# exit 125 (the daemon refused or lost the container) or a lost CLI-daemon connection. gsp07-v1
# lost a run to "error waiting for container: unexpected EOF" (exit 125) on Docker Desktop.
INFRA_FAILURE_SIGNATURES = (" exit 125:", "error waiting for container", "error during connect",
                            "Cannot connect to the Docker daemon")
INFRA_RETRY_PAUSE_S = 30


def infra_failure(record: dict) -> bool:
    failed = record.get("failed") or ""
    return any(signature in failed for signature in INFRA_FAILURE_SIGNATURES)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--workdir", required=True)
    parser.add_argument("--only", type=int, default=None, help="run only the first N runs (smoke)")
    args = parser.parse_args()
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    plan["image_ids"] = {label: sh("docker", "image", "inspect", "--format", "{{.Id}}", image).strip()
                         for label, image in plan["images"].items()}
    # Optional "series": [{"labels": [...], "reps": N}, ...] run one after the other (e.g. a second
    # before/after series that also measures the candidate's own noise); default: one series.
    series = plan.get("series") or [{"labels": list(plan["images"]), "reps": plan["reps"]}]
    runs = []
    rep_base = 0
    for index, block in enumerate(series):
        labels = block["labels"]
        for offset in range(block["reps"]):
            rep = rep_base + offset
            for cell in plan["cells"]:
                rotation = labels[rep % len(labels):] + labels[: rep % len(labels)]
                runs += [(label, dict(cell, series=index), rep) for label in rotation]
        rep_base += block["reps"]
    if args.only:
        runs = runs[: args.only]
    out = Path(args.out)
    workdir = Path(args.workdir)
    for order, (label, cell, rep) in enumerate(runs):
        record = run_one(plan, label, plan["images"][label], cell, rep, order, workdir)
        if infra_failure(record):
            # One retry of the same slot (label, cell, rep, order); the first failure stays on the record.
            first_failure = record["failed"]
            time.sleep(INFRA_RETRY_PAUSE_S)
            record = run_one(plan, label, plan["images"][label], cell, rep, order, workdir)
            record["infra_retry"] = first_failure
        with out.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")
        start = record.get("result", {}).get("start", {})
        print(json.dumps({"order": order, "label": label, "cell": cell, "rep": rep, "failed": record.get("failed"),
                          "ttfb_s": start.get("ttfb_s"), "time_to_start_s": start.get("time_to_start_s"),
                          "stalls": start.get("stalls"), "stall_s": start.get("stall_s")}), flush=True)


if __name__ == "__main__":
    sys.exit(main())

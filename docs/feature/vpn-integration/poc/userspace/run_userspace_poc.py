#!/usr/bin/env python3
"""Userspace-VPN (plug & play) PoC orchestrator — runs on the developer host, drives the local Docker engine.

Air-gapped topology (both networks `--internal`, no internet, no live Proton):
  vpnpoc-wan   172.30.10.0/24 : app 172.30.10.10  <-- WireGuard UDP -->  vpnpoc-wg 172.30.10.2
  vpnpoc-swarm 172.30.20.0/24 : vpnpoc-wg 172.30.20.2 (exit NAT, tracker, port forward 16881)
                                vpnpoc-seeder 172.30.20.3
                                app 172.30.20.10  (direct path on purpose: a bypass must be visible)
The app container runs with DEFAULT Docker privileges (no cap_add, no devices, no sysctl).
Only the simulated exit (vpnpoc-wg) gets NET_ADMIN; it is test infrastructure, not the product.

Usage (from any directory):
  POC_WIREPROXY_DIR=<folder holding the verified wireproxy binary> python run_userspace_poc.py
Writes results/userspace-poc-results.json next to this file; removes every vpnpoc-* object afterwards.
WireGuard test keys live only in the throwaway volume and are never printed.
"""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

IMG = "stremhu-vpn-phase0-poc:rev4"
NETS = {"vpnpoc-wan": "172.30.10.0/24", "vpnpoc-swarm": "172.30.20.0/24"}
VOL = "vpnpoc-share"
HERE = Path(__file__).resolve().parent
WP_DIR = Path(os.environ["POC_WIREPROXY_DIR"]).resolve()
OUT = HERE / "results"
SCENARIOS = [  # direct last: its tracker entry would otherwise be dialled back through the exit
    ("vpn_tcp", "vpn_tcp", None),
    ("vpn_udp_probe", "vpn_udp_probe", None),
    ("killswitch_tunnel", "killswitch", "tunnel"),
    ("killswitch_proxy", "killswitch", "proxy"),
    ("inbound", "inbound", None),
    ("inbound_direct", "inbound_direct", None),
    ("inbound_direct_utp", "inbound_direct_utp", None),
    ("direct", "direct", None),
]


def docker(*args: str, check: bool = True, capture: bool = True) -> str:
    res = subprocess.run(["docker", *args], capture_output=capture, text=True)
    if check and res.returncode != 0:
        raise SystemExit(f"docker {' '.join(args[:3])} failed: {res.stderr.strip()[:400]}")
    return res.stdout.strip() if capture else ""


def cleanup() -> None:
    names = docker("ps", "-a", "--format", "{{.Names}}").splitlines()
    for name in names:
        if name.startswith("vpnpoc-"):
            docker("rm", "-f", name, check=False)
    for net in NETS:
        docker("network", "rm", net, check=False)
    docker("volume", "rm", VOL, check=False)


def check_subnets() -> None:
    for net_id in docker("network", "ls", "-q").splitlines():
        info = json.loads(docker("network", "inspect", net_id))[0]
        if info["Name"] in NETS:
            continue
        for cfg in (info.get("IPAM") or {}).get("Config") or []:
            if cfg.get("Subnet") in NETS.values():
                raise SystemExit(f"subnet {cfg['Subnet']} already used by network {info['Name']}")


def share_read(name: str) -> str:
    return docker("exec", "vpnpoc-seeder", "cat", f"/share/{name}", check=False)


def share_json(name: str, default):
    raw = share_read(name)
    try:
        return json.loads(raw) if raw else default
    except ValueError:
        return default


def wait_file(container: str, name: str, seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if subprocess.run(["docker", "exec", container, "test", "-f", f"/share/{name}"]).returncode == 0:
            return
        time.sleep(1)
    logs = docker("logs", "--tail", "30", container, check=False)
    raise SystemExit(f"{container}: /share/{name} did not appear in {seconds}s\n{logs[-1500:]}")


def seeder_ctl(seq: int, **fields) -> None:
    payload = json.dumps({"seq": seq, **fields})
    docker("exec", "vpnpoc-seeder", "sh", "-c", f"echo '{payload}' > /share/seeder_ctl.json")


def client_ctl(tag: str, seq: int, action: str) -> None:
    payload = json.dumps({"seq": seq, "action": action})
    docker("exec", "vpnpoc-seeder", "sh", "-c", f"echo '{payload}' > /share/{tag}_client_ctl.json")


def progress(tag: str) -> float:
    return float(share_json(f"{tag}_progress.json", {}).get("progress", 0.0))


def run_scenario(tag: str, scenario: str, breaker: str | None, seq: list[int]) -> dict:
    name = f"vpnpoc-app-{tag}"
    seq[0] += 1
    seeder_ctl(seq[0], upload_rate=4 * 1024 * 1024 if breaker else 0, reannounce=True)
    events_before = share_json("server_events.json", {})
    peers_before = share_json("seeder_peers.json", {})
    # Only the direct control may resolve the tracker locally; in VPN runs a local lookup must surface
    # as a DNS packet to 127.0.0.11 (captured) instead of being answered silently from /etc/hosts.
    hosts = ["--add-host", "tracker.poc.test:172.30.20.2"] if scenario == "direct" else []
    docker("create", "--name", name, "--network", "vpnpoc-wan", "--ip", "172.30.10.10", *hosts,
           "--cpus", "2", "--memory", "1g",
           "-e", f"POC_SCENARIO={scenario}", "-e", f"POC_TAG={tag}",
           "-e", f"POC_TIMEOUT={60 if scenario.startswith('inbound_direct') else 300}",
           "-v", f"{VOL}:/share", "-v", f"{HERE}:/poc:ro", "-v", f"{WP_DIR}:/opt/wireproxy:ro",
           "--entrypoint", "python3", IMG, "/poc/us_client.py")
    docker("network", "connect", "--ip", "172.30.20.10", "vpnpoc-swarm", name)
    docker("start", name)
    outage: dict = {}
    if scenario.startswith("inbound"):
        time.sleep(6)
        seq[0] += 1
        target = ["172.30.20.2", 16881] if scenario == "inbound" else ["172.30.20.10", 6881]
        seeder_ctl(seq[0], upload_rate=0, connect=target, outgoing_utp=scenario == "inbound_direct_utp")
    if breaker:
        deadline = time.time() + 200
        while progress(tag) < 0.3 and time.time() < deadline:
            time.sleep(0.5)
        outage["progress_at_break"] = progress(tag)
        if breaker == "tunnel":
            docker("exec", "vpnpoc-wg", "ip", "link", "set", "wg0", "down")
        else:
            client_ctl(tag, 1, "kill_proxy")
        time.sleep(3)
        outage["progress_3s_after_break"] = progress(tag)
        time.sleep(17)
        outage["progress_20s_after_break"] = progress(tag)
        if breaker == "tunnel":
            docker("exec", "vpnpoc-wg", "ip", "link", "set", "wg0", "up")
        else:
            client_ctl(tag, 2, "start_proxy")
        outage["restored_at_progress"] = progress(tag)
    code = docker("wait", name)
    result = share_json(f"{tag}_result.json", {"error": "no result", "exit_code": code,
                                                "logs": docker("logs", "--tail", "20", name, check=False)[-1500:]})
    events_after = share_json("server_events.json", {})
    peers_after = share_json("seeder_peers.json", {})
    result["exit_code"] = code
    result["outage"] = outage or None
    result["server_view"] = {k: events_after.get(k, [])[len(events_before.get(k, [])):] for k in events_after}
    for k, rows in result["server_view"].items():
        result["server_view"][k] = {"count": len(rows), "sources": sorted({r["src"] for r in rows}),
                                    "names": sorted({r["name"] for r in rows if "name" in r})}
    result["seeder_view"] = {k: v for k, v in peers_after.items()
                             if k not in peers_before or v.get("count") != peers_before[k].get("count")
                             or v.get("bytes_up") != peers_before[k].get("bytes_up")}
    docker("rm", "-f", name, check=False)
    return result


def main() -> int:
    binary = WP_DIR / "wireproxy"
    if not binary.is_file():
        raise SystemExit(f"wireproxy binary missing in {WP_DIR}")
    OUT.mkdir(exist_ok=True)
    cleanup()
    check_subnets()
    env = {
        "docker_server": docker("version", "--format", "{{.Server.Version}}"),
        "image": IMG, "image_id": docker("image", "inspect", IMG, "--format", "{{.Id}}"),
        "wireproxy_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        "wireproxy_release": "v1.1.3 linux_amd64 (github.com/whyvl/wireproxy)",
    }
    try:
        for net, subnet in NETS.items():
            docker("network", "create", "--internal", "--subnet", subnet, net)
        docker("volume", "create", VOL)
        docker("run", "--rm", "--network", "none", "-v", f"{VOL}:/share", "--entrypoint", "sh", IMG, "-c",
               "mkdir -p /share/wg && cd /share/wg && umask 077 && wg genkey > server.key && wg pubkey < server.key > server.pub"
               " && wg genkey > client.key && wg pubkey < client.key > client.pub")
        env["kernel"] = docker("run", "--rm", "--network", "none", "--entrypoint", "uname", IMG, "-r")
        docker("create", "--name", "vpnpoc-wg", "--network", "vpnpoc-wan", "--ip", "172.30.10.2",
               "--cap-add", "NET_ADMIN", "--sysctl", "net.ipv4.ip_forward=1",
               "-v", f"{VOL}:/share", "-v", f"{HERE}:/poc:ro", "--entrypoint", "python3", IMG, "/poc/us_wg_server.py")
        docker("network", "connect", "--ip", "172.30.20.2", "vpnpoc-swarm", "vpnpoc-wg")
        docker("start", "vpnpoc-wg")
        wait_file("vpnpoc-wg", "server_ready", 30)
        docker("create", "--name", "vpnpoc-seeder", "--network", "vpnpoc-swarm", "--ip", "172.30.20.3",
               "--add-host", "tracker.poc.test:172.30.20.2", "-e", "POC_SIZE_MIB=256",
               "-v", f"{VOL}:/share", "-v", f"{HERE}:/poc:ro", "--entrypoint", "python3", IMG, "/poc/us_seeder.py")
        docker("start", "vpnpoc-seeder")
        wait_file("vpnpoc-seeder", "seeder_ready", 300)
        results, seq = {}, [0]
        only = set(sys.argv[1:])
        for tag, scenario, breaker in SCENARIOS:
            if only and tag not in only:
                continue
            print(f"== {tag}", flush=True)
            results[tag] = run_scenario(tag, scenario, breaker, seq)
            r = results[tag]
            print(f"   completed={r.get('completed')} sha256_ok={r.get('sha256_ok')} t={r.get('seconds_to_complete')}"
                  f" leak_swarm={r.get('leak_packets_direct_swarm')} leak_wan={r.get('leak_packets_wan_non_tunnel')}"
                  f" dns={r.get('dns_packets')} wp_cpu={r.get('wireproxy_cpu_s')} lt_cpu={r.get('libtorrent_process_cpu_s')}",
                  flush=True)
        path = OUT / "userspace-poc-results.json"
        if only and path.exists():  # partial rerun: keep the other scenarios' latest results
            merged = json.loads(path.read_text(encoding="utf-8"))
            merged["results"].update(results)
            merged.setdefault("partial_reruns", []).append({"environment": env, "scenarios": sorted(results)})
            results, env = merged["results"], merged["environment"]
        path.write_text(json.dumps({"environment": env, "results": results}, indent=1), encoding="utf-8")
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Userspace-VPN PoC — the app side, run in a container with DEFAULT Docker privileges
(no cap_add, no devices, no sysctl): this is the plug & play constraint under test.

wireproxy (userspace WireGuard + netstack, SOCKS5 on 127.0.0.1:25344) carries libtorrent 2.1.1.
Scenarios (POC_SCENARIO):
  direct           positive control: no VPN, libtorrent talks to the swarm directly
  vpn_tcp          proxy only, TCP peers + HTTP tracker; DHT/LSD/UPnP/NAT-PMP/uTP off
  vpn_udp_probe    proxy, but DHT/LSD/UPnP/NAT-PMP/uTP + UDP tracker ON: which mechanisms bypass it?
  killswitch       vpn_tcp; the orchestrator breaks the tunnel or kills wireproxy mid-download
  inbound          vpn_tcp without trackers; the seeder connects in via the forwarded port
                   (wireproxy [TCPServerTunnel] 10.2.0.2:6881 -> 127.0.0.1:6881)
  inbound_direct   vpn_tcp without trackers; the seeder dials the app's REAL address 172.30.20.10:6881
                   (must not deliver any data: no bypass of the tunnel for incoming peers)
The container captures everything it sends except the expected loopback proxy hop and the WireGuard
UDP to the endpoint, and records per-interface byte counters, so any bypass shows up as a leak.
Results: /share/<scenario>_result.json, timeline: /share/<scenario>_progress.json.
"""

import hashlib
import json
import os
import resource
import signal
import socket
import subprocess
import time
from pathlib import Path

import libtorrent as lt

SHARE = Path("/share")
SCENARIO = os.environ["POC_SCENARIO"]
TAG = os.environ.get("POC_TAG", SCENARIO)
TIMEOUT = float(os.environ.get("POC_TIMEOUT", "300"))
WAN_IP, SWARM_IP, WG_ENDPOINT = "172.30.10.10", "172.30.20.10", "172.30.10.2"
SOCKS_PORT = 25344
CLK = os.sysconf("SC_CLK_TCK")
UTP = getattr(lt.peer_info, "utp_socket", 1 << 17)  # peer_flags_t bit 17 in 2.x; not exported by the 2.1.1 binding
VPN = SCENARIO != "direct"


def iface_bytes() -> dict[str, dict[str, int]]:
    out = json.loads(subprocess.check_output(["ip", "-j", "-s", "addr"], text=True))
    result = {}
    for link in out:
        ips = [a.get("local") for a in link.get("addr_info", [])]
        name = "wan" if WAN_IP in ips else "swarm" if SWARM_IP in ips else link["ifname"]
        stats = link.get("stats64", {})
        result[name] = {"rx": stats.get("rx", {}).get("bytes", 0), "tx": stats.get("tx", {}).get("bytes", 0)}
    return result


def proc_cpu(pid: int) -> tuple[float, int]:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    cpu = (int(fields[11]) + int(fields[12])) / CLK
    hwm = next((int(line.split()[1]) for line in Path(f"/proc/{pid}/status").read_text().splitlines()
                if line.startswith("VmHWM:")), 0)
    return cpu, hwm


class Proxy:
    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.cpu_done = 0.0
        self.hwm_kib = 0

    def write_config(self) -> None:
        conf = (
            "[Interface]\n"
            "Address = 10.2.0.2/32\n"
            f"PrivateKey = {(SHARE / 'wg/client.key').read_text().strip()}\n"
            "DNS = 10.2.0.1\n"
            "MTU = 1420\n\n"
            "[Peer]\n"
            f"PublicKey = {(SHARE / 'wg/server.pub').read_text().strip()}\n"
            f"Endpoint = {WG_ENDPOINT}:51820\n"
            "AllowedIPs = 0.0.0.0/0\n"
            "PersistentKeepalive = 25\n\n"
            "[Socks5]\n"
            f"BindAddress = 127.0.0.1:{SOCKS_PORT}\n"
        )
        if SCENARIO == "inbound":
            conf += "\n[TCPServerTunnel]\nListenPort = 6881\nTarget = 127.0.0.1:6881\n"
        path = Path("/tmp/wp.conf")
        path.touch(mode=0o600)
        path.write_text(conf)

    def start(self) -> None:
        binary = Path("/tmp/wireproxy")
        if not binary.exists():  # the bind-mounted copy may lack the exec bit on a Windows host
            binary.write_bytes(Path("/opt/wireproxy/wireproxy").read_bytes())
            binary.chmod(0o755)
        log = open("/tmp/wireproxy.log", "ab")
        self.proc = subprocess.Popen([str(binary), "-c", "/tmp/wp.conf"], stdout=log, stderr=log)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                socket.create_connection(("127.0.0.1", SOCKS_PORT), timeout=0.5).close()
                return
            except OSError:
                time.sleep(0.1)
        raise SystemExit("wireproxy SOCKS5 not listening")

    def sample(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                cpu, hwm = proc_cpu(self.proc.pid)
                self.hwm_kib = max(self.hwm_kib, hwm)
                self._last_cpu = cpu
            except OSError:
                pass

    def kill(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.sample()
            self.cpu_done += getattr(self, "_last_cpu", 0.0)
            self.proc.kill()
            self.proc.wait()
            self._last_cpu = 0.0

    def cpu_seconds(self) -> float:
        self.sample()
        return self.cpu_done + getattr(self, "_last_cpu", 0.0)


def settings() -> dict:
    base = {
        "alert_mask": lt.alert_category.error | lt.alert_category.tracker | lt.alert_category.status
        | lt.alert_category.peer | lt.alert_category.connect,
        "enable_dht": False, "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
        "allow_multiple_connections_per_ip": True,
    }
    if not VPN:
        base.update({"listen_interfaces": "0.0.0.0:6881", "enable_outgoing_utp": True, "enable_incoming_utp": True})
        return base
    base.update({
        "listen_interfaces": "127.0.0.1:6881",
        "proxy_type": lt.proxy_type_t.socks5,
        "proxy_hostname": "127.0.0.1",
        "proxy_port": SOCKS_PORT,
        "proxy_hostnames": True,
        "proxy_peer_connections": True,
        "proxy_tracker_connections": True,
        "enable_outgoing_utp": False, "enable_incoming_utp": False,
    })
    if SCENARIO == "vpn_udp_probe":
        base.update({
            "enable_dht": True, "enable_lsd": True, "enable_upnp": True, "enable_natpmp": True,
            "enable_outgoing_utp": True, "enable_incoming_utp": True,
            "dht_bootstrap_nodes": "dht.poc.test:6881",
        })
    return base


def main() -> None:
    cap_filter = (f"not (tcp port {SOCKS_PORT} and host 127.0.0.1) and not (tcp port 6881 and host 127.0.0.1)"
                  f" and not (udp port 51820 and host {WG_ENDPOINT})")
    cap = subprocess.Popen(["tcpdump", "-i", "any", "-p", "-nn", "-U", "-s", "160", "-w", "/tmp/cap.pcap", cap_filter],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(1.5)
    if cap.poll() is not None:
        raise SystemExit("tcpdump is not capturing (missing CAP_NET_RAW?) - leak analysis would be blind")
    bytes_before = iface_bytes()
    proxy = Proxy()
    if VPN:
        proxy.write_config()
        proxy.start()
    t0 = time.time()
    ses = lt.session(settings())
    atp = lt.load_torrent_file(str(SHARE / "test.torrent"))
    atp.save_path = "/tmp/dl"
    if SCENARIO.startswith("inbound"):
        atp.flags |= lt.torrent_flags.paused
        atp.flags &= ~lt.torrent_flags.auto_managed
    handle = ses.add_torrent(atp)
    if SCENARIO.startswith("inbound"):
        handle.replace_trackers([])
        handle.resume()
    expected = (SHARE / "test.sha256").read_text().strip()
    timeline, alerts, peers_seen = [], [], {}
    last_seq, done_at = -1, None
    sockets: list[str] = []
    ctl_path = SHARE / f"{TAG}_client_ctl.json"
    while time.time() - t0 < TIMEOUT:
        if ctl_path.exists():
            try:
                ctl = json.loads(ctl_path.read_text())
            except ValueError:
                ctl = {}
            if ctl.get("seq", -1) != last_seq:
                last_seq = ctl["seq"]
                if ctl.get("action") == "kill_proxy":
                    proxy.kill()
                elif ctl.get("action") == "start_proxy":
                    proxy.start()
                timeline.append({"t": round(time.time() - t0, 2), "event": ctl.get("action")})
        for a in ses.pop_alerts():
            if len(alerts) < 400 and isinstance(a, (lt.tracker_error_alert, lt.tracker_reply_alert, lt.peer_error_alert,
                                                    lt.peer_disconnected_alert, lt.listen_failed_alert,
                                                    lt.listen_succeeded_alert, lt.dht_bootstrap_alert,
                                                    lt.udp_error_alert, lt.incoming_connection_alert)):
                alerts.append({"t": round(time.time() - t0, 2), "type": type(a).__name__, "msg": a.message()[:200]})
        st = handle.status()
        for p in handle.get_peer_info():
            transport = "utp" if p.flags & UTP else "tcp"
            direction = "out" if p.flags & lt.peer_info.local_connection else "in"
            key = f"{p.ip[0]}:{p.ip[1]} {transport} {direction}"
            peers_seen[key] = max(peers_seen.get(key, 0), p.total_download)
        proxy.sample()
        if not sockets and time.time() - t0 > 5:
            ss = subprocess.run(["ss", "-H", "-tuanp"], capture_output=True, text=True).stdout
            sockets = [" ".join(line.split()) for line in ss.splitlines() if line.strip()][:40]
        timeline.append({"t": round(time.time() - t0, 2), "progress": round(st.progress, 4),
                         "rate": st.download_rate, "peers": st.num_peers})
        (SHARE / f"{TAG}_progress.json").write_text(json.dumps(timeline[-1]))
        if st.is_seeding and done_at is None:
            done_at = time.time() - t0
            break
        time.sleep(0.5)
    wall = time.time() - t0
    self_cpu = resource.getrusage(resource.RUSAGE_SELF)  # before hashing, so it measures the transfer only
    proxy_cpu = proxy.cpu_seconds()
    files = list(Path("/tmp/dl").rglob("*.mkv"))
    digest = None
    if files and done_at:
        h = hashlib.sha256()
        with files[0].open("rb") as f:
            for block in iter(lambda: f.read(1 << 20), b""):
                h.update(block)
        digest = h.hexdigest()
    proxy.kill()
    time.sleep(1.0)
    bytes_after = iface_bytes()
    cap.send_signal(signal.SIGINT)
    cap.wait(timeout=10)

    def packets(expr: str) -> list[str]:
        out = subprocess.run(["tcpdump", "-nn", "-r", "/tmp/cap.pcap", expr], capture_output=True, text=True)
        return [line for line in out.stdout.splitlines() if line.strip()]

    # IPv4 only: ARP (gratuitous ARP at link-up, endpoint MAC lookup) is link housekeeping, not egress.
    direct_swarm = packets(f"ip and host {SWARM_IP}")
    wan_other = packets(f"ip and host {WAN_IP}")
    dns = packets("port 53")
    dns_names = sorted({w for line in subprocess.run(["tcpdump", "-nn", "-vv", "-r", "/tmp/cap.pcap", "udp and src host 127.0.0.11"], capture_output=True, text=True).stdout.split("q: ")[1:] for w in [line.split(" ")[1] if " " in line else line]})
    ipv6 = packets("ip6 and not icmp6")
    arp_count = len(packets("arp"))
    result = {
        "scenario": SCENARIO, "tag": TAG, "vpn": VPN, "completed": done_at is not None,
        "seconds_to_complete": round(done_at, 2) if done_at else None, "wall_seconds": round(wall, 2),
        "sha256_ok": digest == expected if digest else False,
        "size_mib": round(sum(f.stat().st_size for f in files) / 2**20, 1) if files else 0,
        "libtorrent_process_cpu_s": round(self_cpu.ru_utime + self_cpu.ru_stime, 2),
        "libtorrent_process_maxrss_mib": round(self_cpu.ru_maxrss / 1024, 1),
        "wireproxy_cpu_s": round(proxy_cpu, 2) if VPN else None,
        "wireproxy_hwm_mib": round(proxy.hwm_kib / 1024, 1) if VPN else None,
        "iface_bytes_delta": {k: {d: bytes_after.get(k, {}).get(d, 0) - v[d] for d in ("rx", "tx")}
                              for k, v in bytes_before.items()},
        "leak_packets_direct_swarm": len(direct_swarm), "leak_samples_direct_swarm": direct_swarm[:8],
        "leak_packets_wan_non_tunnel": len(wan_other), "leak_samples_wan_non_tunnel": wan_other[:8],
        "dns_packets": len(dns), "dns_samples": dns[:12], "dns_local_query_names": dns_names,
        "sockets_at_5s": sockets,
        "ipv6_non_icmp_packets": len(ipv6), "ipv6_samples": ipv6[:8], "arp_packets": arp_count,
        "peers_seen": peers_seen, "alerts": alerts[:120],
        "timeline": timeline[::4] + timeline[-1:],
    }
    (SHARE / f"{TAG}_result.json").write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Phase 0 Rev. 4 Retest PoC for P0-2 (Task 8):
Validates the Rev. 4 corrected Section 14.1 `nftables` Kill Switch + Docker `127.0.0.11:53` DNS DNAT/SNAT
+ Mount-Free / Sysctl-Free `table local` Source-IP Override + `fork+exec` Netns Helper Execution.

Fixes verified vs. Rev. 3 P0-2 FAIL:
  1. `type nat hook output priority -100; policy accept;` (fixes `nft -c -f -` rc=1 on `priority dstnat`).
  2. Zero `/proc/sys` writes (`EROFS`-safe in non-privileged Docker): uses RTNETLINK
     `ip route replace local <resolver>/32 dev lo src <WG_CLIENT_IPV4> table local` + `ip -6 addr flush dev lo`.
  3. `ct status dnat ip daddr $VPN_DNS_IPV4_SET udp/tcp dport 53 counter accept` placed BEFORE
     `oifname "lo" udp/tcp dport 53 counter drop` (fixes Linux `__ip_local_out()` frozen `oifname="lo"` drop).
  4. `fork+exec` (`subprocess.run(..., pass_fds=(netns_fd,))`) instead of multi-threaded `os.fork()`.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from poc_p0_1_netns_privdrop_and_helper import drop_all_privileges_and_verify  # noqa: E402

POC_VERSION = "rev4-1.0.0"

# Exact Rev. 4 Section 14.1 ruleset template
REV4_SECTION_14_1_NFT_TEMPLATE = """# Renderelt, validált IPv4 konstansok (ipaddress.IPv4Address által ellenőrizve):
define WG_IFACE = "{wg_iface}"
define WG_CLIENT_IPV4 = {wg_client_ipv4}
define VPN_GATEWAY_IPV4 = {vpn_gateway_ipv4}
define VPN_PRIMARY_DNS_IPV4 = {vpn_primary_dns_ipv4}
define VPN_DNS_IPV4_SET = {{ {vpn_dns_ipv4_set} }}
define INIT_RESOLVER_IPV4_SET = {{ {init_resolver_ipv4_set} }}

# 1. Scoped idempotens táblacsere (NINCS globális 'flush ruleset'!)
table ip stremhu_dns_nat
delete table ip stremhu_dns_nat

table ip stremhu_dns_nat {{
    chain output {{
        type nat hook output priority -100; policy accept;
        ip daddr $INIT_RESOLVER_IPV4_SET udp dport 53 counter dnat to $VPN_PRIMARY_DNS_IPV4:53
        ip daddr $INIT_RESOLVER_IPV4_SET tcp dport 53 counter dnat to $VPN_PRIMARY_DNS_IPV4:53
    }}

    chain postrouting {{
        type nat hook postrouting priority srcnat; policy accept;
        oifname $WG_IFACE ip saddr != $WG_CLIENT_IPV4 counter snat to $WG_CLIENT_IPV4
    }}
}}

table inet stremhu_killswitch
delete table inet stremhu_killswitch

table inet stremhu_killswitch {{
    set bt_listen_port {{
        type inet_service
    }}

    chain input {{
        type filter hook input priority filter; policy drop;
        meta nfproto ipv6 counter drop
        ct state invalid counter drop
        iifname "lo" meta nfproto ipv4 accept
        iifname != $WG_IFACE counter drop
        ct state established,related accept
        ip protocol icmp icmp type {{ echo-reply, destination-unreachable, time-exceeded }} accept
        iifname $WG_IFACE tcp dport @bt_listen_port accept
        iifname $WG_IFACE udp dport @bt_listen_port accept
    }}

    chain forward {{
        type filter hook forward priority filter; policy drop;
    }}

    chain output {{
        type filter hook output priority filter; policy drop;
        meta nfproto ipv6 counter drop
        ct state invalid counter drop

        # Linux __ip_local_out() freezes nf_hook_state.out="lo" across NF_INET_LOCAL_OUT even after output DNAT
        # reroutes skb_dst to stremhu-wg0; explicitly accept DNAT-rewritten VPN DNS before oifname "lo" drop!
        ct status dnat ip daddr $VPN_DNS_IPV4_SET udp dport 53 counter accept
        ct status dnat ip daddr $VPN_DNS_IPV4_SET tcp dport 53 counter accept

        oifname "lo" udp dport 53 counter drop
        oifname "lo" tcp dport 53 counter drop
        oifname "lo" meta nfproto ipv4 accept

        oifname != $WG_IFACE counter drop

        udp dport 53 ip daddr $VPN_DNS_IPV4_SET accept
        tcp dport 53 ip daddr $VPN_DNS_IPV4_SET accept
        udp dport 53 counter drop
        tcp dport 53 counter drop

        udp dport 5351 ip daddr $VPN_GATEWAY_IPV4 accept
        udp dport 5351 counter drop
        tcp dport 5351 counter drop

        ip daddr $VPN_GATEWAY_IPV4 ip protocol icmp icmp type echo-request accept

        ip daddr {{
            10.0.0.0/8,
            100.64.0.0/10,
            169.254.0.0/16,
            172.16.0.0/12,
            192.168.0.0/16,
            224.0.0.0/4,
            255.255.255.255/32
        }} counter drop

        oifname $WG_IFACE meta nfproto ipv4 accept
    }}
}}
"""


def build_dns_query_packet(hostname: str, txid: int = 0x4242) -> bytes:
    hdr = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(lbl)]) + lbl.encode("ascii") for lbl in hostname.strip(".").split(".")) + b"\x00"
    return hdr + qname + struct.pack("!HH", 1, 1)


def run_netns_helper_fork_exec(netns_fd: int, action: str, payload: dict[str, object]) -> object:
    """Safe fork+exec helper invocation (zero multi-threaded os.fork() DeprecationWarning!)."""
    os.set_inheritable(netns_fd, True)
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--netns-helper", str(netns_fd), action],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            pass_fds=(netns_fd,),
            check=False,
        )
    finally:
        os.set_inheritable(netns_fd, False)
    if proc.returncode != 0:
        raise RuntimeError(f"netns helper {action} failed rc={proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()}")
    res = json.loads(proc.stdout)
    if not res.get("ok"):
        raise RuntimeError(str(res.get("err")))
    return res["res"]


class InitNetHealthHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        body = json.dumps(
            {
                "service": "stremhu-init-net-http",
                "port": 7070,
                "netns_ino": os.stat("/proc/self/ns/net").st_ino,
                "client_ip": self.client_address[0],
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def setup_killswitch_in_isolated_ns(
    expected_init_id: tuple[int, int],
    expected_worker_id: tuple[int, int],
    init_resolvers: list[str],
) -> dict[str, object]:
    cur = os.stat("/proc/self/ns/net")
    cur_id = (cur.st_dev, cur.st_ino)
    if cur_id == expected_init_id:
        raise RuntimeError("VpnKillSwitchError: Refusing to apply nftables in init_net!")
    if cur_id != expected_worker_id:
        raise RuntimeError(f"VpnKillSwitchError: Unexpected netns {cur_id} != {expected_worker_id}")
    ifaces = [n for _i, n in socket.if_nameindex()]
    for forbidden in ("eth0", "docker0"):
        if forbidden in ifaces:
            raise RuntimeError(f"VpnKillSwitchError: Forbidden host/bridge iface {forbidden} in isolated netns!")

    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
    subprocess.run(["ip", "-6", "addr", "flush", "dev", "lo"], check=True)

    rendered = REV4_SECTION_14_1_NFT_TEMPLATE.format(
        wg_iface="stremhu-wg0",
        wg_client_ipv4="10.2.0.2",
        vpn_gateway_ipv4="10.2.0.1",
        vpn_primary_dns_ipv4="10.2.0.1",
        vpn_dns_ipv4_set="10.2.0.1",
        init_resolver_ipv4_set=", ".join(init_resolvers),
    )
    assert "flush ruleset" not in "\n".join(
        ln for ln in rendered.splitlines() if not ln.lstrip().startswith("#")
    )

    chk1 = subprocess.run(["nft", "-c", "-f", "-"], input=rendered, capture_output=True, text=True, check=False)
    load1 = subprocess.run(["nft", "-f", "-"], input=rendered, capture_output=True, text=True, check=False)
    chk2 = subprocess.run(["nft", "-c", "-f", "-"], input=rendered, capture_output=True, text=True, check=False)
    load2 = subprocess.run(["nft", "-f", "-"], input=rendered, capture_output=True, text=True, check=False)

    json_dump = subprocess.run(["nft", "-j", "list", "ruleset"], capture_output=True, text=True, check=True)
    policies: dict[str, str] = {}
    for item in json.loads(json_dump.stdout).get("nftables", []):
        ch = item.get("chain")
        if ch and ch.get("table") == "stremhu_killswitch":
            policies[ch["name"]] = ch.get("policy", "MISSING")

    return {
        "zero_proc_sys_writes_performed": True,
        "first_nft_check_rc": chk1.returncode,
        "first_nft_check_stderr": chk1.stderr.strip(),
        "first_nft_load_rc": load1.returncode,
        "first_nft_load_stderr": load1.stderr.strip(),
        "second_idempotent_nft_check_rc": chk2.returncode,
        "second_idempotent_nft_check_stderr": chk2.stderr.strip(),
        "second_idempotent_nft_load_rc": load2.returncode,
        "second_idempotent_nft_load_stderr": load2.stderr.strip(),
        "killswitch_chain_policies": policies,
    }


def configure_wg_in_isolated_ns(peer_info: dict[str, object], init_resolvers: list[str]) -> dict[str, object]:
    subprocess.run(["ip", "addr", "add", "10.2.0.2/32", "dev", "stremhu-wg0"], check=True)
    wg_conf = (
        f"[Interface]\nPrivateKey = {peer_info['client_private_key']}\n\n"
        f"[Peer]\nPublicKey = {peer_info['server_public_key']}\n"
        f"Endpoint = {peer_info['peer_bridge_ip']}:{peer_info['peer_listen_port']}\n"
        f"AllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n"
    )
    subprocess.run(["wg", "setconf", "stremhu-wg0", "/dev/stdin"], input=wg_conf, text=True, check=True)
    subprocess.run(["ip", "link", "set", "mtu", "1420", "up", "dev", "stremhu-wg0"], check=True)
    subprocess.run(["ip", "route", "add", "default", "dev", "stremhu-wg0"], check=True)

    for res_ip in init_resolvers:
        if res_ip.startswith("127."):
            subprocess.run(
                ["ip", "route", "replace", "local", f"{res_ip}/32", "dev", "lo", "src", "10.2.0.2", "table", "local"],
                check=True,
            )

    subprocess.run(
        ["nft", "add", "element", "inet", "stremhu_killswitch", "bt_listen_port", "{ 6881 }"],
        check=True,
    )
    return {
        "rtnetlink_ifaces": sorted(n for _i, n in socket.if_nameindex()),
        "routes": subprocess.check_output(["ip", "route"], text=True).strip().splitlines(),
        "route_get_127_0_0_11": subprocess.check_output(["ip", "route", "get", "127.0.0.11"], text=True).strip(),
    }


def netns_helper_cli(netns_fd: int, action: str) -> int:
    payload = json.loads(sys.stdin.read() or "{}")
    try:
        os.setns(netns_fd, os.CLONE_NEWNET)
        if action == "setup_ks":
            res = setup_killswitch_in_isolated_ns(
                tuple(payload["init_id"]),
                tuple(payload["worker_id"]),
                list(payload["init_resolvers"]),
            )
        elif action == "config_wg":
            res = configure_wg_in_isolated_ns(dict(payload["peer_info"]), list(payload["init_resolvers"]))
        elif action == "http_json":
            with urllib.request.urlopen(str(payload["url"]), timeout=3.0) as resp:
                res = json.loads(resp.read().decode())
        elif action == "del_wg":
            subprocess.run(["ip", "link", "del", "dev", "stremhu-wg0"], check=True)
            res = {"deleted": True}
        elif action == "nft_dump":
            res = subprocess.check_output(["nft", "list", "ruleset"], text=True)
        else:
            raise ValueError(f"Unknown action: {action}")
        print(json.dumps({"ok": True, "res": res}))
        return 0
    except BaseException as exc:
        print(json.dumps({"ok": False, "err": repr(exc)}))
        return 1


def worker_subprocess_main(sock_fd: int) -> int:
    sock = socket.socket(fileno=sock_fd)
    ppid = os.getppid()
    os.unshare(os.CLONE_NEWNET)
    st_ns = os.stat("/proc/self/ns/net")
    sock.sendall(
        (json.dumps({"stage": "unshared", "netns_dev": st_ns.st_dev, "netns_ino": st_ns.st_ino}) + "\n").encode()
    )
    f_in = sock.makefile("r")
    if f_in.readline().strip() != "drop_and_run_tests":
        return 90

    if sorted(n for _i, n in socket.if_nameindex()) != ["lo", "stremhu-wg0"]:
        os._exit(78)

    priv_audit = drop_all_privileges_and_verify(10001, 10001, ppid)
    import libtorrent as lt  # noqa: PLC0415

    results: dict[str, object] = {
        "uid": priv_audit["status"]["Uid"].split("\t")[0],
        "cap_eff": priv_audit["status"]["CapEff"],
    }

    gai = socket.getaddrinfo("tracker.poc.test", 6969, socket.AF_INET, socket.SOCK_STREAM)
    results["glibc_udp_dns_127_0_0_11"] = {"ok": True, "ip": gai[0][4][0]}

    q_pkt = build_dns_query_packet("egress.poc.test", txid=0x5151)
    with socket.create_connection(("127.0.0.11", 53), timeout=3.0) as tcp_dns:
        tcp_dns.sendall(struct.pack("!H", len(q_pkt)) + q_pkt)
        resp_len = struct.unpack("!H", tcp_dns.recv(2))[0]
        resp_body = tcp_dns.recv(resp_len)
    results["tcp_dns_127_0_0_11"] = {"ok": True, "ip": socket.inet_ntoa(resp_body[-4:])}

    with tempfile.TemporaryDirectory(prefix="lt_p0_2_rev4_") as tmpdir:
        dummy_file = Path(tmpdir) / "sample.bin"
        dummy_file.write_bytes(b"A" * 65536)
        fs = lt.file_storage()
        lt.add_files(fs, str(dummy_file))
        ct = lt.create_torrent(fs, 65536)
        ct.add_tracker("http://tracker.poc.test:6969/announce")
        lt.set_piece_hashes(ct, tmpdir)
        torrent_bytes = lt.bencode(ct.generate())

        sess = lt.session(
            {
                "listen_interfaces": "10.2.0.2:6881",
                "outgoing_interfaces": "stremhu-wg0",
                "enable_dht": False,
                "enable_lsd": False,
                "enable_upnp": False,
                "enable_natpmp": False,
                "alert_mask": lt.alert.category_t.all_categories,
            }
        )
        ti = lt.torrent_info(lt.bdecode(torrent_bytes))
        sess.add_torrent({"ti": ti, "save_path": tmpdir})
        tracker_reply = None
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline and tracker_reply is None:
            sess.wait_for_alert(200)
            for a in sess.pop_alerts():
                if isinstance(a, lt.tracker_reply_alert):
                    tracker_reply = {"url": a.tracker_url(), "num_peers": a.num_peers}
                    break
        results["libtorrent_tracker_announce"] = tracker_reply
        del sess

    neg_probes: dict[str, dict[str, object]] = {}

    def _udp_probe(label: str, family: int, host: str, port: int, pkt: bytes) -> None:
        s = socket.socket(family, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        try:
            s.sendto(pkt, (host, port))
            data, _ = s.recvfrom(512)
            neg_probes[label] = {"blocked": False, "received_len": len(data)}
        except OSError as exc:
            neg_probes[label] = {"blocked": True, "errno_name": errno.errorcode.get(exc.errno, type(exc).__name__)}
        finally:
            s.close()

    def _tcp_probe(label: str, family: int, host: str, port: int) -> None:
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(0.6)
        try:
            s.connect((host, port))
            neg_probes[label] = {"blocked": False}
        except OSError as exc:
            neg_probes[label] = {"blocked": True, "errno_name": errno.errorcode.get(exc.errno, type(exc).__name__)}
        finally:
            s.close()

    dns_probe_pkt = build_dns_query_packet("tracker.poc.test", txid=0x9999)
    _udp_probe("rogue_udp_dns_8_8_8_8_53", socket.AF_INET, "8.8.8.8", 53, dns_probe_pkt)
    _tcp_probe("rogue_tcp_dns_9_9_9_9_53", socket.AF_INET, "9.9.9.9", 53)
    _udp_probe("loopback_udp_dns_127_0_0_1_53", socket.AF_INET, "127.0.0.1", 53, dns_probe_pkt)
    _tcp_probe("loopback_tcp_dns_127_0_0_1_53", socket.AF_INET, "127.0.0.1", 53)
    _udp_probe("ipv6_udp_dns_2606_4700_53", socket.AF_INET6, "2606:4700:4700::1111", 53, dns_probe_pkt)
    _udp_probe("lan_router_natpmp_192_168_1_1_5351", socket.AF_INET, "192.168.1.1", 5351, b"\x00\x00")
    _tcp_probe("rfc1918_10_0_0_1_80", socket.AF_INET, "10.0.0.1", 80)
    _tcp_probe("vpn_gateway_non_service_port_10_2_0_1_80", socket.AF_INET, "10.2.0.1", 80)
    results["negative_probes_with_wg_up"] = dict(neg_probes)

    sock.sendall((json.dumps({"stage": "tests_phase1_done", "results": results}) + "\n").encode())

    if f_in.readline().strip() == "test_after_wg_deleted":
        neg_probes.clear()
        _udp_probe("after_wg_del_udp_egress", socket.AF_INET, "198.51.100.42", 6969, b"ping")
        _tcp_probe("after_wg_del_tcp_egress", socket.AF_INET, "198.51.100.42", 6969)
        sock.sendall((json.dumps({"probes": dict(neg_probes)}) + "\n").encode())
    return 0


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--netns-helper":
        return netns_helper_cli(int(sys.argv[2]), sys.argv[3])
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker-child":
        return worker_subprocess_main(int(sys.argv[2]))

    script_sha256 = hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()
    peer_info = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))

    resolv_text = Path("/etc/resolv.conf").read_text(encoding="utf-8")
    init_resolvers = [
        ln.split()[1] for ln in resolv_text.splitlines() if ln.strip().startswith("nameserver ") and "." in ln.split()[1]
    ]
    init_stat = os.stat("/proc/self/ns/net")
    init_id = (init_stat.st_dev, init_stat.st_ino)
    init_routes_before = subprocess.check_output(["ip", "route"], text=True).strip()
    init_nft_before = subprocess.check_output(["nft", "list", "ruleset"], text=True)
    eth0_json = json.loads(subprocess.check_output(["ip", "-4", "-j", "addr", "show", "dev", "eth0"], text=True))
    self_bridge_ip = eth0_json[0]["addr_info"][0]["local"]

    http_7070 = ThreadingHTTPServer(("0.0.0.0", 7070), InitNetHealthHandler)
    threading.Thread(target=http_7070.serve_forever, daemon=True).start()

    eth0_pcap = "/tmp/eth0_p0_2_rev4.pcap"
    tcpdump_eth0 = subprocess.Popen(
        ["tcpdump", "-ni", "eth0", "-U", "-w", eth0_pcap],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.25)

    # Negative check: Inode guard rejects init_net execution
    inode_guard_rejected = False
    try:
        setup_killswitch_in_isolated_ns(init_id, init_id, init_resolvers)
    except RuntimeError as exc:
        inode_guard_rejected = "Refusing to apply nftables in init_net" in str(exc)

    p_sock, c_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    os.set_inheritable(c_sock.fileno(), True)
    worker_proc = subprocess.Popen(
        [sys.executable, "-W", "ignore::DeprecationWarning", __file__, "--worker-child", str(c_sock.fileno())],
        pass_fds=(c_sock.fileno(),),
    )
    c_sock.close()
    f_worker = p_sock.makefile("r")
    unshared_info = json.loads(f_worker.readline())
    worker_ns_id = (unshared_info["netns_dev"], unshared_info["netns_ino"])
    netns_fd = os.open(f"/proc/{worker_proc.pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC)

    nft_res = run_netns_helper_fork_exec(
        netns_fd,
        "setup_ks",
        {"init_id": list(init_id), "worker_id": list(worker_ns_id), "init_resolvers": init_resolvers},
    )

    subprocess.run(["ip", "link", "add", "dev", "stremhu-wg0", "type", "wireguard"], check=True)
    os.set_inheritable(netns_fd, True)
    subprocess.run(
        ["ip", "link", "set", "dev", "stremhu-wg0", "netns", f"/proc/self/fd/{netns_fd}"],
        pass_fds=(netns_fd,),
        check=True,
    )
    os.set_inheritable(netns_fd, False)

    wg_topo = run_netns_helper_fork_exec(
        netns_fd,
        "config_wg",
        {"peer_info": peer_info, "init_resolvers": init_resolvers},
    )

    p_sock.sendall(b"drop_and_run_tests\n")
    phase1 = json.loads(f_worker.readline())["results"]

    bridge_probe = run_netns_helper_fork_exec(
        netns_fd,
        "http_json",
        {"url": f"http://198.51.100.42:6969/probe-init-7070?target={self_bridge_ip}"},
    )
    run_netns_helper_fork_exec(netns_fd, "del_wg", {})
    p_sock.sendall(b"test_after_wg_deleted\n")
    after_del = json.loads(f_worker.readline())

    p_sock.close()
    worker_proc.wait(timeout=5)
    os.close(netns_fd)

    time.sleep(0.25)
    tcpdump_eth0.send_signal(signal.SIGTERM)
    tcpdump_eth0.wait(timeout=3)

    init_routes_after = subprocess.check_output(["ip", "route"], text=True).strip()
    init_nft_after = subprocess.check_output(["nft", "list", "ruleset"], text=True)
    eth0_packets = subprocess.check_output(["tcpdump", "-nn", "-r", eth0_pcap], text=True, stderr=subprocess.DEVNULL).splitlines()
    eth0_leaks = [
        ln
        for ln in eth0_packets
        if (".53:" in ln or ".53 " in ln or ".6969" in ln or ".6881" in ln or "8.8.8.8" in ln or "9.9.9.9" in ln or "198.51.100.42" in ln)
    ]

    all_neg_blocked = all(v["blocked"] for v in phase1["negative_probes_with_wg_up"].values()) and all(
        v["blocked"] for v in after_del["probes"].values()
    )
    gate_pass = all(
        [
            inode_guard_rejected,
            nft_res["first_nft_check_rc"] == 0,
            nft_res["first_nft_load_rc"] == 0,
            nft_res["second_idempotent_nft_check_rc"] == 0,
            nft_res["second_idempotent_nft_load_rc"] == 0,
            nft_res["killswitch_chain_policies"] == {"input": "drop", "forward": "drop", "output": "drop"},
            phase1["glibc_udp_dns_127_0_0_11"]["ip"] == "198.51.100.42",
            phase1["tcp_dns_127_0_0_11"]["ip"] == "198.51.100.42",
            phase1["libtorrent_tracker_announce"] is not None,
            all_neg_blocked,
            bridge_probe["bridge_probe_ok"] is True,
            init_routes_after == init_routes_before,
            init_nft_after == init_nft_before,
            len(eth0_leaks) == 0,
        ]
    )

    out = {
        "poc_id": "P0-2-Rev4-Retest",
        "poc_version": POC_VERSION,
        "script_sha256": script_sha256,
        "executed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "gate_status": "PASS" if gate_pass else "FAIL",
        "netns_inode_guard_negative_test_passed": inode_guard_rejected,
        "rev4_nftables_validation": nft_res,
        "isolated_netns_topology": wg_topo,
        "worker_results": phase1,
        "after_wg_deleted_probes": after_del["probes"],
        "bridge_7070_http_isolation_probe": bridge_probe,
        "init_net_invariance": {
            "routes_byte_identical": init_routes_after == init_routes_before,
            "nft_ruleset_byte_identical": init_nft_after == init_nft_before,
            "nft_sha256": hashlib.sha256(init_nft_after.encode()).hexdigest(),
        },
        "eth0_pcap_audit": {
            "eth0_total_packets": len(eth0_packets),
            "eth0_dns_or_cleartext_leak_packets": len(eth0_leaks),
        },
    }
    print(json.dumps(out, indent=2))
    return 0 if gate_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())

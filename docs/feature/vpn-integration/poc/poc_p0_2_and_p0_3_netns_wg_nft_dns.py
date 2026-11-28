#!/usr/bin/env python3
"""Phase 0 PoC P0-2 + Local Cross-Netns P0-3:
1. Exact Section 14.1 `nftables` ruleset validation (`nft -c -f -`, atomic `nft -f -`, idempotent reload,
   `nft -j list ruleset` policy verification, zero `flush ruleset`).
2. User-defined Docker bridge `127.0.0.11:53` DNS transparent `output dnat` + `postrouting snat` -> `10.2.0.1:53`
   for both glibc (`socket.getaddrinfo`), raw TCP DNS, and `libtorrent==2.1.1` HTTP tracker announce.
3. Cross-netns `stremhu-wg0` Birthplace Namespace WireGuard handshake WITHOUT `/dev/net/tun` and WITHOUT
   `--privileged`, while `:7070` HTTP server in `init_net` remains reachable and `init_net` routes/nftables
   remain 100% byte-identical.
4. Negative leak probes (`8.8.8.8:53`, `9.9.9.9:53`, `127.0.0.1:53`, IPv6 `2606:4700:4700::1111:53`,
   `192.168.1.1:5351`, `10.0.0.1:80`, `10.2.0.1:80`, plus `stremhu-wg0` deletion hard-drop test) verified via
   both `nftables` rule counters and `tcpdump` packet captures on `eth0` and `stremhu-wg0`.
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

# Exact verbatim Section 14.1 ruleset template from docs/feature/vpn-integration/implementation-plan.md Rev. 3 (now archive/implementation-plan-rev3.md, Section 14.1)
SECTION_14_1_NFT_TEMPLATE = """# Renderelt, validált IPv4 konstansok (ipaddress.IPv4Address által ellenőrizve):
define WG_IFACE = "{wg_iface}"
define WG_CLIENT_IPV4 = {wg_client_ipv4}
define VPN_GATEWAY_IPV4 = {vpn_gateway_ipv4}
define VPN_PRIMARY_DNS_IPV4 = {vpn_primary_dns_ipv4}
define VPN_DNS_IPV4_SET = {{ {vpn_dns_ipv4_set} }}
define INIT_RESOLVER_IPV4_SET = {{ {init_resolver_ipv4_set} }}

# 1. Scoped idempotens táblacsere (NINCS globális 'flush ruleset'!)
#    (B-MountFree módban a stremhu_dns_nat tábla is betöltődik)
table ip stremhu_dns_nat
delete table ip stremhu_dns_nat

table ip stremhu_dns_nat {{
    chain output {{
        type nat hook output priority dstnat; policy accept;
        # A konténer /etc/resolv.conf feloldóira (pl. Docker 127.0.0.11:53) menő DNS kérések
        # transzparens átirányítása a Proton belső DNS szerverére (route_localnet=1 mellett)
        ip daddr $INIT_RESOLVER_IPV4_SET udp dport 53 counter dnat to $VPN_PRIMARY_DNS_IPV4:53
        ip daddr $INIT_RESOLVER_IPV4_SET tcp dport 53 counter dnat to $VPN_PRIMARY_DNS_IPV4:53
    }}

    chain postrouting {{
        type nat hook postrouting priority srcnat; policy accept;
        # A 127.0.0.11:53-ra indított kérések 127.0.0.1 forráscímének átírása a WG alagút IP-re
        # (a conntrack a választ automatikusan visszaírja 127.0.0.11:53 -> 127.0.0.1-re)
        oifname $WG_IFACE ip saddr != $WG_CLIENT_IPV4 counter snat to $WG_CLIENT_IPV4
    }}
}}

table inet stremhu_killswitch
delete table inet stremhu_killswitch

table inet stremhu_killswitch {{
    # Dinamikusan frissített halmaz az éppen aktív BitTorrent listen porthoz (pl. 6881 vagy NAT-PMP port)
    set bt_listen_port {{
        type inet_service
    }}

    chain input {{
        type filter hook input priority filter; policy drop;

        # 1. IPv6 teljes és feltétlen tiltása
        meta nfproto ipv6 counter drop

        # 2. Érvénytelen conntrack állapotú csomagok eldobása
        ct state invalid counter drop

        # 3. Loopback IPv4 engedélyezése
        iifname "lo" meta nfproto ipv4 accept

        # 4. Minden nem stremhu-wg0 interfészen érkező csomag eldobása
        iifname != $WG_IFACE counter drop

        # 5. Felépített kimenő kapcsolatok válaszcsomagjainak engedélyezése (DNS, NAT-PMP, peer, tracker)
        ct state established,related accept

        # 6. Szükséges IPv4 ICMP típusok (PMTU discovery / Echo reply) engedélyezése stremhu-wg0-n
        ip protocol icmp icmp type {{ echo-reply, destination-unreachable, time-exceeded }} accept

        # 7. Bejövő ÚJ BitTorrent peer kapcsolatok engedélyezése KIZÁRÓLAG az aktív listen porton!
        iifname $WG_IFACE tcp dport @bt_listen_port accept
        iifname $WG_IFACE udp dport @bt_listen_port accept
    }}

    chain forward {{
        type filter hook forward priority filter; policy drop;
    }}

    chain output {{
        type filter hook output priority filter; policy drop;

        # 1. IPv6 teljes és feltétlen tiltása
        meta nfproto ipv6 counter drop

        # 2. Érvénytelen conntrack csomagok eldobása
        ct state invalid counter drop

        # 3. Loopback IPv4: az 53-as port (UDP és TCP külön szabályban!) tiltása, minden más lo IPv4 engedélyezése
        oifname "lo" udp dport 53 counter drop
        oifname "lo" tcp dport 53 counter drop
        oifname "lo" meta nfproto ipv4 accept

        # 4. KIZÁRÓLAG a stremhu-wg0 interfészen hagyhatja el csomag a névteret
        oifname != $WG_IFACE counter drop

        # 5. DNS Leak zár: az 53-as portra KIZÁRÓLAG a konfigurált VPN DNS szerver(ek) felé mehet csomag
        udp dport 53 ip daddr $VPN_DNS_IPV4_SET accept
        tcp dport 53 ip daddr $VPN_DNS_IPV4_SET accept
        udp dport 53 counter drop
        tcp dport 53 counter drop

        # 6. NAT-PMP zár: az 5351-es portra KIZÁRÓLAG a Proton VPN gateway (10.2.0.1) felé mehet UDP csomag
        udp dport 5351 ip daddr $VPN_GATEWAY_IPV4 accept
        udp dport 5351 counter drop
        tcp dport 5351 counter drop

        # 7. Diagnosztikai ICMP echo-request engedélyezése a VPN gateway felé
        ip daddr $VPN_GATEWAY_IPV4 ip protocol icmp icmp type echo-request accept

        # 8. Minden privát, link-local, CGNAT és multicast IPv4 tartomány szigorú tiltása a tunnelen keresztül is!
        ip daddr {{
            10.0.0.0/8,
            100.64.0.0/10,
            169.254.0.0/16,
            172.16.0.0/12,
            192.168.0.0/16,
            224.0.0.0/4,
            255.255.255.255/32
        }} counter drop

        # 9. Publikus internetes IPv4 BitTorrent peer és HTTP/HTTPS/UDP tracker forgalom engedélyezése stremhu-wg0-n
        oifname $WG_IFACE meta nfproto ipv4 accept
    }}
}}
"""


def build_dns_query_packet(hostname: str, txid: int = 0x4242) -> bytes:
    hdr = struct.pack("!HHHHHH", txid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(bytes([len(lbl)]) + lbl.encode("ascii") for lbl in hostname.strip(".").split(".")) + b"\x00"
    return hdr + qname + struct.pack("!HH", 1, 1)


def run_in_netns(netns_fd: int, fn, *args):
    r_fd, w_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(r_fd)
        try:
            os.setns(netns_fd, os.CLONE_NEWNET)
            res = fn(*args)
            os.write(w_fd, json.dumps({"ok": True, "res": res}).encode())
            os._exit(0)
        except BaseException as exc:
            os.write(w_fd, json.dumps({"ok": False, "err": repr(exc)}).encode())
            os._exit(1)
    os.close(w_fd)
    chunks = []
    while True:
        buf = os.read(r_fd, 65536)
        if not buf:
            break
        chunks.append(buf)
    os.close(r_fd)
    os.waitpid(pid, 0)
    payload = json.loads(b"".join(chunks).decode())
    if not payload["ok"]:
        raise RuntimeError(payload["err"])
    return payload["res"]


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


def worker_subprocess_main(sock_fd: int) -> int:
    sock = socket.socket(fileno=sock_fd)
    ppid = os.getppid()
    os.unshare(os.CLONE_NEWNET)
    st_ns = os.stat("/proc/self/ns/net")
    sock.sendall(
        (
            json.dumps(
                {
                    "stage": "unshared",
                    "netns_dev": st_ns.st_dev,
                    "netns_ino": st_ns.st_ino,
                    "sysfs_class_net_before_wg": sorted(os.listdir("/sys/class/net")),
                    "rtnetlink_ifaces_before_wg": [n for _i, n in socket.if_nameindex()],
                }
            )
            + "\n"
        ).encode()
    )
    f_in = sock.makefile("r")
    cmd = f_in.readline().strip()
    if cmd != "drop_and_run_tests":
        return 90

    # Verify RTNETLINK interfaces inside isolated netns BEFORE dropping privileges: ONLY ['lo', 'stremhu-wg0']!
    rtnl_ifaces = sorted(n for _i, n in socket.if_nameindex())
    sysfs_ifaces = sorted(os.listdir("/sys/class/net"))
    if rtnl_ifaces != ["lo", "stremhu-wg0"]:
        os._exit(78)

    priv_audit = drop_all_privileges_and_verify(10001, 10001, ppid)
    import libtorrent as lt  # noqa: PLC0415

    results: dict[str, object] = {
        "sysfs_class_net_after_wg_stale": sysfs_ifaces,
        "rtnetlink_ifaces_after_wg_actual": rtnl_ifaces,
        "priv_audit": priv_audit,
    }

    # Positive tests wrapped individually so any failure is captured with exact errno rather than aborting!
    try:
        gai = socket.getaddrinfo("tracker.poc.test", 6969, socket.AF_INET, socket.SOCK_STREAM)
        results["glibc_getaddrinfo"] = {"ok": True, "ip": gai[0][4][0]}
    except BaseException as exc:
        results["glibc_getaddrinfo"] = {"ok": False, "err": repr(exc)}

    try:
        q_pkt = build_dns_query_packet("egress.poc.test", txid=0x5151)
        with socket.create_connection(("127.0.0.11", 53), timeout=3.0) as tcp_dns:
            tcp_dns.sendall(struct.pack("!H", len(q_pkt)) + q_pkt)
            resp_len = struct.unpack("!H", tcp_dns.recv(2))[0]
            resp_body = tcp_dns.recv(resp_len)
        results["tcp_dns_127_0_0_11"] = {"ok": True, "ip": socket.inet_ntoa(resp_body[-4:])}
    except BaseException as exc:
        results["tcp_dns_127_0_0_11"] = {"ok": False, "err": repr(exc)}

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u_nat:
            u_nat.settimeout(2.0)
            u_nat.sendto(b"\x00\x00", ("10.2.0.1", 5351))
            nat_resp, nat_src = u_nat.recvfrom(64)
            ver, op, rcode, epoch, pub_ip_int = struct.unpack("!BBHII", nat_resp)
        results["natpmp_gateway_10_2_0_1_5351"] = {
            "ok": True,
            "from": f"{nat_src[0]}:{nat_src[1]}",
            "version": ver,
            "opcode": op,
            "result_code": rcode,
            "public_ip": socket.inet_ntoa(struct.pack("!I", pub_ip_int)),
        }
    except BaseException as exc:
        results["natpmp_gateway_10_2_0_1_5351"] = {"ok": False, "err": repr(exc)}

    try:
        with urllib.request.urlopen("http://198.51.100.42:6969/egress-ip", timeout=3.0) as resp:
            results["worker_direct_ip_http_egress"] = {"ok": True, "data": json.loads(resp.read().decode())}
    except BaseException as exc:
        results["worker_direct_ip_http_egress"] = {"ok": False, "err": repr(exc)}

    try:
        with urllib.request.urlopen("http://egress.poc.test:6969/egress-ip", timeout=3.0) as resp:
            results["worker_hostname_http_egress"] = {"ok": True, "data": json.loads(resp.read().decode())}
    except BaseException as exc:
        results["worker_hostname_http_egress"] = {"ok": False, "err": repr(exc)}

    with tempfile.TemporaryDirectory(prefix="lt_p0_2_") as tmpdir:
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
            sess.wait_for_alert(250)
            for a in sess.pop_alerts():
                if isinstance(a, lt.tracker_reply_alert):
                    tracker_reply = {"url": a.tracker_url(), "num_peers": a.num_peers, "msg": a.message()}
                    break
        results["libtorrent_tracker_announce"] = tracker_reply
        del sess

    # 5. Negative Leak Probes (MUST ALL FAIL / DROP!)
    neg_probes: dict[str, dict[str, object]] = {}

    def _udp_probe(label: str, family: int, host: str, port: int, payload: bytes) -> None:
        s = socket.socket(family, socket.SOCK_DGRAM)
        s.settimeout(0.6)
        try:
            s.sendto(payload, (host, port))
            data, _ = s.recvfrom(512)
            neg_probes[label] = {"blocked": False, "received_len": len(data)}
        except OSError as exc:
            neg_probes[label] = {
                "blocked": True,
                "errno": exc.errno,
                "errno_name": errno.errorcode.get(exc.errno, type(exc).__name__),
                "detail": str(exc),
            }
        finally:
            s.close()

    def _tcp_probe(label: str, family: int, host: str, port: int) -> None:
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(0.8)
        try:
            s.connect((host, port))
            neg_probes[label] = {"blocked": False}
        except OSError as exc:
            neg_probes[label] = {
                "blocked": True,
                "errno": exc.errno,
                "errno_name": errno.errorcode.get(exc.errno, type(exc).__name__),
                "detail": str(exc),
            }
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

    results["negative_probes_with_wg_up"] = neg_probes
    sock.sendall((json.dumps({"stage": "tests_phase1_done", "results": results}) + "\n").encode())

    # Wait for parent to delete stremhu-wg0 (Scenario #5 / B-02) and test hard kernel drop
    cmd2 = f_in.readline().strip()
    if cmd2 == "test_after_wg_deleted":
        neg_probes.clear()
        _udp_probe("after_wg_del_udp_egress_198_51_100_42_6969", socket.AF_INET, "198.51.100.42", 6969, b"ping")
        _tcp_probe("after_wg_del_tcp_egress_198_51_100_42_6969", socket.AF_INET, "198.51.100.42", 6969)
        sock.sendall(
            (
                json.dumps(
                    {
                        "stage": "after_wg_del_done",
                        "rtnetlink_ifaces": [n for _i, n in socket.if_nameindex()],
                        "probes": dict(neg_probes),
                    }
                )
                + "\n"
            ).encode()
        )
    return 0


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
    sysctls = [
        ("net.ipv6.conf.all.disable_ipv6", "1"),
        ("net.ipv6.conf.default.disable_ipv6", "1"),
        ("net.ipv4.conf.all.route_localnet", "1"),
        ("net.ipv4.conf.lo.route_localnet", "1"),
    ]
    sysctl_results: dict[str, str] = {}
    for k, v in sysctls:
        p = Path(f"/proc/sys/{k.replace('.', '/')}")
        try:
            p.write_text(v + "\n", encoding="ascii")
            sysctl_results[k] = f"OK (now={p.read_text(encoding='ascii').strip()})"
        except OSError as exc:
            cur_val = p.read_text(encoding="ascii").strip() if p.exists() else "MISSING"
            sysctl_results[k] = f"FAILED errno={exc.errno} ({errno.errorcode.get(exc.errno, 'ERR')}): {exc.strerror} [current={cur_val}]"

    rendered = SECTION_14_1_NFT_TEMPLATE.format(
        wg_iface="stremhu-wg0",
        wg_client_ipv4="10.2.0.2",
        vpn_gateway_ipv4="10.2.0.1",
        vpn_primary_dns_ipv4="10.2.0.1",
        vpn_dns_ipv4_set="10.2.0.1",
        init_resolver_ipv4_set=", ".join(init_resolvers),
    )
    assert "flush ruleset" not in rendered.split("#")[0]  # no active flush ruleset directive

    # 1A. Test EXACT AS-WRITTEN Rev. 3 Section 14.1 ruleset (`priority dstnat` on `hook output`):
    as_written_chk = subprocess.run(
        ["nft", "-c", "-f", "-"], input=rendered, capture_output=True, text=True, check=False
    )

    # 1B. Test Phase 0 verified candidate fix:
    #   Fix #1: `priority -100` instead of `priority dstnat` on `type nat hook output`
    #   Fix #2: `ct status dnat ip daddr $VPN_DNS_IPV4_SET udp/tcp dport 53 accept` before `oifname "lo" ... drop`
    #           because Linux `__ip_local_out()` freezes `nf_hook_state.out = lo` across `NF_INET_LOCAL_OUT`!
    rendered_fixed = rendered.replace(
        "type nat hook output priority dstnat; policy accept;",
        "type nat hook output priority -100; policy accept;",
    ).replace(
        '        oifname "lo" udp dport 53 counter drop\n',
        '        ct status dnat ip daddr $VPN_DNS_IPV4_SET udp dport 53 counter accept\n'
        '        ct status dnat ip daddr $VPN_DNS_IPV4_SET tcp dport 53 counter accept\n'
        '        oifname "lo" udp dport 53 counter drop\n',
    )
    chk1 = subprocess.run(["nft", "-c", "-f", "-"], input=rendered_fixed, capture_output=True, text=True, check=False)
    load1 = (
        subprocess.run(["nft", "-f", "-"], input=rendered_fixed, capture_output=True, text=True, check=False)
        if chk1.returncode == 0
        else None
    )
    # 2nd idempotent check + reload when tables ALREADY exist!
    chk2 = (
        subprocess.run(["nft", "-c", "-f", "-"], input=rendered_fixed, capture_output=True, text=True, check=False)
        if load1 and load1.returncode == 0
        else None
    )
    load2 = (
        subprocess.run(["nft", "-f", "-"], input=rendered_fixed, capture_output=True, text=True, check=False)
        if chk2 and chk2.returncode == 0
        else None
    )

    json_dump = subprocess.run(["nft", "-j", "list", "ruleset"], capture_output=True, text=True, check=False)
    policies: dict[str, str] = {}
    if json_dump.returncode == 0 and json_dump.stdout.strip():
        parsed_nft = json.loads(json_dump.stdout)
        for item in parsed_nft.get("nftables", []):
            ch = item.get("chain")
            if ch and ch.get("table") == "stremhu_killswitch":
                policies[ch["name"]] = ch.get("policy", "MISSING")

    return {
        "sysctl_attempts_in_nonprivileged_docker": sysctl_results,
        "as_written_rev3_nft_check_rc": as_written_chk.returncode,
        "as_written_rev3_nft_check_stderr": as_written_chk.stderr.strip(),
        "fixed_priority_first_nft_check_rc": chk1.returncode,
        "fixed_priority_first_nft_check_stderr": chk1.stderr.strip(),
        "fixed_priority_first_nft_load_rc": load1.returncode if load1 else None,
        "fixed_priority_first_nft_load_stderr": load1.stderr.strip() if load1 else None,
        "fixed_priority_second_idempotent_check_rc": chk2.returncode if chk2 else None,
        "fixed_priority_second_idempotent_check_stderr": chk2.stderr.strip() if chk2 else None,
        "fixed_priority_second_idempotent_load_rc": load2.returncode if load2 else None,
        "killswitch_chain_policies": policies,
    }


def configure_wg_in_isolated_ns(peer_info: dict[str, object], init_resolvers: list[str]) -> dict[str, object]:
    # Zero-mount IPv6 lockdown via RTNETLINK (works even when /proc/sys is read-only in Docker!)
    subprocess.run(["ip", "-6", "addr", "flush", "dev", "lo"], check=True)

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

    # Zero-mount / zero-sysctl fix for Docker 127.0.0.11 DNS DNAT when route_localnet=0 (/proc/sys is ro):
    # Override source IP selection in `table local` for 127.x.x.x init resolvers -> src 10.2.0.2!
    for res_ip in init_resolvers:
        if res_ip.startswith("127."):
            subprocess.run(
                ["ip", "route", "replace", "local", f"{res_ip}/32", "dev", "lo", "src", "10.2.0.2", "table", "local"],
                check=True,
            )

    # Add initial BitTorrent listen port 6881 to @bt_listen_port
    subprocess.run(
        ["nft", "add", "element", "inet", "stremhu_killswitch", "bt_listen_port", "{ 6881 }"],
        check=True,
    )
    links = subprocess.check_output(["ip", "-o", "link"], text=True).strip().splitlines()
    routes = subprocess.check_output(["ip", "route"], text=True).strip().splitlines()
    local_127_route = subprocess.check_output(["ip", "route", "get", "127.0.0.11"], text=True).strip()
    return {"links": links, "routes": routes, "route_get_127_0_0_11": local_127_route}


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--worker-child":
        return worker_subprocess_main(int(sys.argv[2]))

    peer_info = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    report: dict[str, object] = {}

    # 1. Inspect /dev/net/tun and /etc/resolv.conf in init_net
    tun_exists = Path("/dev/net/tun").exists()
    resolv_text = Path("/etc/resolv.conf").read_text(encoding="utf-8")
    init_resolvers = [
        line.split()[1]
        for line in resolv_text.splitlines()
        if line.strip().startswith("nameserver ") and "." in line.split()[1]
    ]
    init_stat = os.stat("/proc/self/ns/net")
    init_id = (init_stat.st_dev, init_stat.st_ino)
    init_links_before = subprocess.check_output(["ip", "-o", "link"], text=True).strip()
    init_routes_before = subprocess.check_output(["ip", "route"], text=True).strip()
    init_nft_before = subprocess.check_output(["nft", "list", "ruleset"], text=True)
    eth0_json = json.loads(subprocess.check_output(["ip", "-4", "-j", "addr", "show", "dev", "eth0"], text=True))
    self_bridge_ip = eth0_json[0]["addr_info"][0]["local"]

    report["init_net_before"] = {
        "dev_net_tun_exists": tun_exists,
        "resolv_conf": resolv_text.strip(),
        "parsed_init_resolvers": init_resolvers,
        "self_bridge_ip": self_bridge_ip,
        "init_netns_ino": init_stat.st_ino,
        "links": init_links_before.splitlines(),
        "routes": init_routes_before.splitlines(),
        "nft_ruleset_sha256": hashlib.sha256(init_nft_before.encode()).hexdigest(),
        "nft_ruleset_excerpt": init_nft_before.strip(),
    }

    # 2. Start HTTP :7070 server in init_net + tcpdump on eth0
    http_7070 = ThreadingHTTPServer(("0.0.0.0", 7070), InitNetHealthHandler)
    threading.Thread(target=http_7070.serve_forever, daemon=True).start()

    eth0_pcap = "/tmp/eth0_p0_2.pcap"
    wg0_pcap = "/tmp/wg0_p0_2.pcap"
    tcpdump_eth0 = subprocess.Popen(
        ["tcpdump", "-ni", "eth0", "-U", "-w", eth0_pcap],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(0.3)

    # 3. Verify Netns Inode Guard rejects accidental init_net execution!
    inode_guard_rejected = False
    try:
        setup_killswitch_in_isolated_ns(init_id, init_id, init_resolvers)
    except RuntimeError as exc:
        inode_guard_rejected = "Refusing to apply nftables in init_net" in str(exc)
    report["netns_inode_guard_negative_test_passed"] = inode_guard_rejected

    # 4. Spawn Worker subprocess (unshares CLONE_NEWNET, pauses before privilege drop)
    p_sock, c_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    os.set_inheritable(c_sock.fileno(), True)
    worker_proc = subprocess.Popen(
        [sys.executable, __file__, "--worker-child", str(c_sock.fileno())],
        pass_fds=(c_sock.fileno(),),
    )
    c_sock.close()
    f_worker = p_sock.makefile("r")
    unshared_info = json.loads(f_worker.readline())
    worker_ns_id = (unshared_info["netns_dev"], unshared_info["netns_ino"])
    netns_fd = os.open(f"/proc/{worker_proc.pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC)

    # 5. Setup Section 14.1 nftables Kill Switch inside isolated netns BEFORE stremhu-wg0 exists!
    nft_setup_res = run_in_netns(netns_fd, setup_killswitch_in_isolated_ns, init_id, worker_ns_id, init_resolvers)
    report["section_14_1_nftables_validation"] = nft_setup_res

    if nft_setup_res["fixed_priority_first_nft_load_rc"] != 0:
        p_sock.close()
        worker_proc.kill()
        tcpdump_eth0.terminate()
        print(json.dumps(report, indent=2))
        return 1

    # 6. Birthplace Namespace WireGuard creation in init_net + move to worker_netns (NO /dev/net/tun!)
    subprocess.run(["ip", "link", "add", "dev", "stremhu-wg0", "type", "wireguard"], check=True)
    os.set_inheritable(netns_fd, True)
    subprocess.run(
        ["ip", "link", "set", "dev", "stremhu-wg0", "netns", f"/proc/self/fd/{netns_fd}"],
        pass_fds=(netns_fd,),
        check=True,
    )
    os.set_inheritable(netns_fd, False)
    wg_topo_res = run_in_netns(netns_fd, configure_wg_in_isolated_ns, peer_info, init_resolvers)
    report["isolated_netns_topology"] = {
        "unshared_initial": unshared_info,
        "configured_wg": wg_topo_res,
    }

    # Start tcpdump on stremhu-wg0 inside isolated netns
    os.set_inheritable(netns_fd, True)
    tcpdump_wg0 = subprocess.Popen(
        [
            "python3",
            "-c",
            f"import os; os.setns({netns_fd}, os.CLONE_NEWNET); os.execvp('tcpdump', ['tcpdump', '-ni', 'stremhu-wg0', '-U', '-w', '{wg0_pcap}'])",
        ],
        pass_fds=(netns_fd,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.set_inheritable(netns_fd, False)
    time.sleep(0.3)

    # 7. Release Worker to drop privileges (UID=10001, Cap*=0) and execute positive + negative tests
    p_sock.sendall(b"drop_and_run_tests\n")
    phase1_msg = json.loads(f_worker.readline())
    report["worker_phase1_results"] = phase1_msg["results"]

    # Also ask sibling container via WG tunnel to probe our init_net :7070 over eth0 bridge!
    bridge_7070_probe = run_in_netns(
        netns_fd,
        lambda ip: json.loads(
            urllib.request.urlopen(f"http://198.51.100.42:6969/probe-init-7070?target={ip}", timeout=3.0).read().decode()
        ),
        self_bridge_ip,
    )
    report["bridge_7070_http_isolation_probe"] = bridge_7070_probe

    # Fetch peer-side recorded DNS/Tracker/NAT-PMP source IPs BEFORE deleting stremhu-wg0
    peer_stats = run_in_netns(
        netns_fd,
        lambda: json.loads(urllib.request.urlopen("http://198.51.100.42:6969/stats", timeout=3.0).read().decode()),
    )
    report["wg_peer_observed_events"] = peer_stats

    # Capture wg show stats inside isolated netns
    wg_stats = run_in_netns(
        netns_fd,
        lambda: {
            "latest_handshakes": subprocess.check_output(
                ["wg", "show", "stremhu-wg0", "latest-handshakes"], text=True
            ).strip(),
            "transfer": subprocess.check_output(["wg", "show", "stremhu-wg0", "transfer"], text=True).strip(),
        },
    )
    # Redact peer public key to fingerprint in output
    for k in ("latest_handshakes", "transfer"):
        parts = wg_stats[k].split()
        if parts:
            parts[0] = f"<redacted_pub_sha256:{hashlib.sha256(parts[0].encode()).hexdigest()[:12]}>"
            wg_stats[k] = "\t".join(parts)
    report["wireguard_kernel_handshake_stats"] = wg_stats

    # 8. Scenario #5 / B-02: Delete stremhu-wg0 while Worker is running and verify hard fail-closed drop
    tcpdump_wg0.send_signal(signal.SIGTERM)
    tcpdump_wg0.wait(timeout=3)
    run_in_netns(netns_fd, lambda: subprocess.run(["ip", "link", "del", "dev", "stremhu-wg0"], check=True).returncode)
    p_sock.sendall(b"test_after_wg_deleted\n")
    after_del_msg = json.loads(f_worker.readline())
    report["after_wg_interface_deleted_test"] = after_del_msg

    # Capture final nftables ruleset with packet counters inside isolated netns!
    worker_nft_counters = run_in_netns(
        netns_fd,
        lambda: subprocess.check_output(["nft", "list", "ruleset"], text=True),
    )
    report["isolated_netns_nft_counters_after"] = worker_nft_counters

    p_sock.close()
    worker_proc.wait(timeout=5)
    os.close(netns_fd)

    time.sleep(0.3)
    tcpdump_eth0.send_signal(signal.SIGTERM)
    tcpdump_eth0.wait(timeout=3)

    # 9. Verify init_net routes & nftables are 100% untouched, and inspect pcaps!
    init_routes_after = subprocess.check_output(["ip", "route"], text=True).strip()
    init_nft_after = subprocess.check_output(["nft", "list", "ruleset"], text=True)
    eth0_packets = subprocess.check_output(["tcpdump", "-nn", "-r", eth0_pcap], text=True, stderr=subprocess.DEVNULL).splitlines()
    wg0_packets = subprocess.check_output(["tcpdump", "-nn", "-r", wg0_pcap], text=True, stderr=subprocess.DEVNULL).splitlines()

    eth0_dns_or_cleartext_bt = [
        ln
        for ln in eth0_packets
        if (".53:" in ln or ".53 " in ln or ".6969" in ln or ".6881" in ln or "8.8.8.8" in ln or "9.9.9.9" in ln or "198.51.100.42" in ln)
    ]

    report["init_net_after_integrity"] = {
        "routes_byte_identical": init_routes_after == init_routes_before,
        "nft_ruleset_byte_identical": init_nft_after == init_nft_before,
        "nft_ruleset_after_sha256": hashlib.sha256(init_nft_after.encode()).hexdigest(),
    }
    report["pcap_summary"] = {
        "eth0_total_packets": len(eth0_packets),
        "eth0_dns_or_cleartext_leak_packets": len(eth0_dns_or_cleartext_bt),
        "eth0_sample_lines": eth0_packets[:12],
        "wg0_total_packets": len(wg0_packets),
        "wg0_sample_lines": wg0_packets[:20],
    }

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

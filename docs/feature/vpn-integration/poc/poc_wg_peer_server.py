#!/usr/bin/env python3
"""Isolated Local WireGuard Peer + Authoritative DNS (UDP/TCP :53) + BitTorrent HTTP Tracker (:6969)
+ RFC 6886 NAT-PMP Responder (:5351) for Phase 0 P0-2 / Local P0-3 PoC.

Runs strictly inside an --internal air-gapped Docker test bridge container (NO --privileged, NO /dev/net/tun).
"""

from __future__ import annotations

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

DNS_RECORDS = {
    "tracker.poc.test.": "198.51.100.42",
    "egress.poc.test.": "198.51.100.42",
}

EVENTS_LOCK = threading.Lock()
EVENTS: dict[str, list[dict[str, object]]] = {
    "dns_queries": [],
    "tracker_announces": [],
    "natpmp_requests": [],
    "bridge_http_7070_checks": [],
}


def _parse_qname(packet: bytes, offset: int = 12) -> tuple[str, int]:
    labels: list[str] = []
    idx = offset
    while idx < len(packet):
        length = packet[idx]
        if length == 0:
            idx += 1
            break
        if (length & 0xC0) == 0xC0:
            ptr = ((length & 0x3F) << 8) | packet[idx + 1]
            sub_name, _ = _parse_qname(packet, ptr)
            labels.append(sub_name.rstrip("."))
            idx += 2
            break
        idx += 1
        labels.append(packet[idx : idx + length].decode("ascii", errors="replace"))
        idx += length
    return ".".join(labels) + ".", idx


def build_dns_response(query: bytes, peer_addr: tuple[str, int], proto: str) -> bytes:
    if len(query) < 12:
        return b""
    txid, _flags, qdcount, _an, _ns, _ar = struct.unpack("!HHHHHH", query[:12])
    if qdcount < 1:
        return b""
    qname, idx = _parse_qname(query, 12)
    if idx + 4 > len(query):
        return b""
    qtype, qclass = struct.unpack("!HH", query[idx : idx + 4])
    question_section = query[12 : idx + 4]

    with EVENTS_LOCK:
        EVENTS["dns_queries"].append(
            {
                "proto": proto,
                "src_ip": peer_addr[0],
                "src_port": peer_addr[1],
                "qname": qname,
                "qtype": qtype,
            }
        )

    ip_str = DNS_RECORDS.get(qname.lower())
    if ip_str and qtype == 1 and qclass == 1:
        # Standard response, AA=1, RA=1, NOERROR (0x8580)
        hdr = struct.pack("!HHHHHH", txid, 0x8580, 1, 1, 0, 0)
        ans = struct.pack("!HHHLH", 0xC00C, 1, 1, 60, 4) + socket.inet_aton(ip_str)
        return hdr + question_section + ans
    if ip_str and qclass == 1:
        # NODATA (NOERROR, 0 answers) e.g. for AAAA queries
        hdr = struct.pack("!HHHHHH", txid, 0x8580, 1, 0, 0, 0)
        return hdr + question_section
    # NXDOMAIN (0x8583)
    hdr = struct.pack("!HHHHHH", txid, 0x8583, 1, 0, 0, 0)
    return hdr + question_section


def serve_udp_dns(bind_ip: str, port: int = 53) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_ip, port))
    while True:
        data, addr = sock.recvfrom(4096)
        resp = build_dns_response(data, addr, "udp")
        if resp:
            sock.sendto(resp, addr)


def serve_tcp_dns(bind_ip: str, port: int = 53) -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((bind_ip, port))
    srv.listen(16)
    while True:
        conn, addr = srv.accept()
        try:
            hdr = conn.recv(2)
            if len(hdr) == 2:
                msg_len = struct.unpack("!H", hdr)[0]
                body = b""
                while len(body) < msg_len:
                    chunk = conn.recv(msg_len - len(body))
                    if not chunk:
                        break
                    body += chunk
                resp = build_dns_response(body, addr, "tcp")
                if resp:
                    conn.sendall(struct.pack("!H", len(resp)) + resp)
        finally:
            conn.close()


def serve_natpmp(bind_ip: str, port: int = 5351) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((bind_ip, port))
    t0 = time.monotonic()
    while True:
        data, addr = sock.recvfrom(2048)
        epoch = int(time.monotonic() - t0) + 100
        if len(data) == 2 and data[0] == 0 and data[1] == 0:
            with EVENTS_LOCK:
                EVENTS["natpmp_requests"].append({"op": 0, "src_ip": addr[0], "len": len(data)})
            resp = struct.pack("!BBHII", 0, 128, 0, epoch, struct.unpack("!I", socket.inet_aton("198.51.100.42"))[0])
            sock.sendto(resp, addr)
        elif len(data) == 12 and data[0] == 0 and data[1] in (1, 2):
            ver, op, _res, int_port, sug_port, lifetime = struct.unpack("!BBHHHI", data)
            ext_port = sug_port if 1024 <= sug_port <= 65535 else 54321
            eff_int = int_port if int_port > 0 else 0
            eff_life = 60 if lifetime > 0 else 0
            with EVENTS_LOCK:
                EVENTS["natpmp_requests"].append(
                    {
                        "op": op,
                        "src_ip": addr[0],
                        "len": len(data),
                        "internal_port": int_port,
                        "suggested_port": sug_port,
                        "assigned_port": ext_port,
                    }
                )
            resp = struct.pack("!BBHIHHI", ver, 128 + op, 0, epoch, eff_int, ext_port, eff_life)
            sock.sendto(resp, addr)


class TrackerAndStatsHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/announce":
            qs = parse_qs(parsed.query, keep_blank_values=True)
            with EVENTS_LOCK:
                EVENTS["tracker_announces"].append(
                    {
                        "src_ip": self.client_address[0],
                        "src_port": self.client_address[1],
                        "port_param": qs.get("port", [""])[0],
                        "user_agent": self.headers.get("User-Agent", ""),
                    }
                )
            body = b"d8:completei1e10:incompletei0e8:intervali60e5:peers0:e"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/egress-ip":
            body = json.dumps({"seen_client_ip": self.client_address[0], "vpn_exit_ip": "198.51.100.42"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == "/probe-init-7070":
            qs = parse_qs(parsed.query)
            target = qs.get("target", ["172.31.250.20"])[0]
            import urllib.request  # noqa: PLC0415

            with urllib.request.urlopen(f"http://{target}:7070/health", timeout=3.0) as resp:
                payload = json.loads(resp.read().decode())
            with EVENTS_LOCK:
                EVENTS["bridge_http_7070_checks"].append(payload)
            out = json.dumps({"bridge_probe_ok": True, "response": payload}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
        elif parsed.path == "/stats":
            with EVENTS_LOCK:
                body = json.dumps(EVENTS, indent=2).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main() -> int:
    srv_priv = subprocess.check_output(["wg", "genkey"], text=True).strip()
    srv_pub = subprocess.check_output(["wg", "pubkey"], input=srv_priv + "\n", text=True).strip()
    cli_priv = subprocess.check_output(["wg", "genkey"], text=True).strip()
    cli_pub = subprocess.check_output(["wg", "pubkey"], input=cli_priv + "\n", text=True).strip()

    # Configure WireGuard server interface wg-peer0 + simulated public IP 198.51.100.42 on lo
    subprocess.run(["ip", "addr", "add", "198.51.100.42/32", "dev", "lo"], check=True)
    subprocess.run(["ip", "link", "add", "dev", "wg-peer0", "type", "wireguard"], check=True)
    subprocess.run(["ip", "addr", "add", "10.2.0.1/32", "dev", "wg-peer0"], check=True)
    wg_conf = (
        f"[Interface]\nPrivateKey = {srv_priv}\nListenPort = 51820\n\n"
        f"[Peer]\nPublicKey = {cli_pub}\nAllowedIPs = 10.2.0.2/32\n"
    )
    subprocess.run(["wg", "setconf", "wg-peer0", "/dev/stdin"], input=wg_conf, text=True, check=True)
    subprocess.run(["ip", "link", "set", "up", "dev", "wg-peer0"], check=True)
    subprocess.run(["ip", "route", "add", "10.2.0.2/32", "dev", "wg-peer0"], check=True)

    threading.Thread(target=serve_udp_dns, args=("10.2.0.1", 53), daemon=True).start()
    threading.Thread(target=serve_tcp_dns, args=("10.2.0.1", 53), daemon=True).start()
    threading.Thread(target=serve_natpmp, args=("10.2.0.1", 5351), daemon=True).start()

    http_srv = ThreadingHTTPServer(("198.51.100.42", 6969), TrackerAndStatsHandler)
    threading.Thread(target=http_srv.serve_forever, daemon=True).start()

    # Determine bridge IPv4 on eth0
    eth0_json = json.loads(subprocess.check_output(["ip", "-4", "-j", "addr", "show", "dev", "eth0"], text=True))
    bridge_ip = eth0_json[0]["addr_info"][0]["local"]

    info = {
        "ready": True,
        "peer_bridge_ip": bridge_ip,
        "peer_listen_port": 51820,
        "server_public_key": srv_pub,
        "client_private_key": cli_priv,
        "client_public_key": cli_pub,
        "client_tunnel_ip": "10.2.0.2/32",
        "vpn_gateway_ip": "10.2.0.1",
        "vpn_dns_ip": "10.2.0.1",
    }
    Path("/tmp/peer_ready.json").write_text(json.dumps(info), encoding="utf-8")
    print(json.dumps(info), flush=True)

    stop_evt = threading.Event()
    signal.signal(signal.SIGTERM, lambda _s, _f: stop_evt.set())
    signal.signal(signal.SIGINT, lambda _s, _f: stop_evt.set())
    stop_evt.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

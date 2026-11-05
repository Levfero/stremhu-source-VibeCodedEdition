#!/usr/bin/env python3
"""Phase 0 PoC P0-4: Proton VPN NAT-PMP Protocol & Wire-Format Verification Harness.

- Strict Honesty (Rules 8 & 9): If `PROTON_P2P_WG_CONF` is not supplied in the environment,
  this script refuses to simulate a live Proton P2P result and outputs `"gate_status": "NOT RUN"`.
- In addition, it executes an offline wire-byte equivalence check between our RFC 6886 struct
  codec and the reference `/usr/bin/natpmpc` binary (`natpmpc -g 127.0.0.1 -a 1 0 udp 60` / `tcp 60`)
  plus privileged-port (<1024) and non-zero result_code rejection tests.
- When `PROTON_P2P_WG_CONF` (and optional `PROTON_NON_P2P_WG_CONF`) ARE provided on an isolated
  Linux test VM, `--live` executes the 10-cycle x 45s live Proton gateway test on `10.2.0.1:5351`.
"""

from __future__ import annotations

import os
import socket
import struct
import subprocess
import sys
import threading
import time
import json
from pathlib import Path

NATPMP_VERSION = 0
OP_PUBLIC_ADDRESS = 0
OP_MAP_UDP = 1
OP_MAP_TCP = 2


class NatPmpProtocolError(RuntimeError):
    pass


def encode_public_ip_request() -> bytes:
    pkt = struct.pack("!BB", NATPMP_VERSION, OP_PUBLIC_ADDRESS)
    assert len(pkt) == 2
    return pkt


def decode_public_ip_response(data: bytes) -> dict[str, object]:
    if len(data) != 12:
        raise NatPmpProtocolError(f"Expected 12-byte Opcode 0 response, got {len(data)} bytes")
    ver, resp_op, result_code, epoch, ip_u32 = struct.unpack("!BBHII", data)
    if ver != NATPMP_VERSION or resp_op != 128:
        raise NatPmpProtocolError(f"Invalid header ver={ver} op={resp_op}")
    if result_code != 0:
        raise NatPmpProtocolError(f"NAT-PMP result_code={result_code}")
    return {
        "version": ver,
        "opcode": resp_op,
        "result_code": result_code,
        "epoch": epoch,
        "public_ip": socket.inet_ntoa(struct.pack("!I", ip_u32)),
    }


def encode_mapping_request(
    proto_opcode: int,
    internal_port: int = 1,
    suggested_external_port: int = 0,
    lifetime_seconds: int = 60,
) -> bytes:
    if proto_opcode not in (OP_MAP_UDP, OP_MAP_TCP):
        raise ValueError(f"Invalid mapping opcode: {proto_opcode}")
    pkt = struct.pack(
        "!BBHHHI",
        NATPMP_VERSION,
        proto_opcode,
        0,
        internal_port,
        suggested_external_port,
        lifetime_seconds,
    )
    assert len(pkt) == 12
    return pkt


def decode_mapping_response(data: bytes, expected_req_opcode: int, allow_release: bool = False) -> dict[str, object]:
    if len(data) != 16:
        raise NatPmpProtocolError(f"Expected 16-byte mapping response, got {len(data)} bytes")
    ver, resp_op, result_code, epoch, int_port, ext_port, lifetime = struct.unpack("!BBHIHHI", data)
    if ver != NATPMP_VERSION or resp_op != 128 + expected_req_opcode:
        raise NatPmpProtocolError(f"Unexpected header ver={ver} resp_op={resp_op}")
    if result_code != 0:
        raise NatPmpProtocolError(f"NAT-PMP error result_code={result_code}")
    if not allow_release and not (1024 <= ext_port <= 65535):
        raise NatPmpProtocolError(f"Rejected privileged/invalid external port {ext_port} (< 1024)")
    return {
        "version": ver,
        "opcode": resp_op,
        "result_code": result_code,
        "epoch": epoch,
        "internal_port": int_port,
        "external_port": ext_port,
        "lifetime": lifetime,
    }


def verify_wire_equivalence_with_natpmpc() -> dict[str, object]:
    """Captures actual UDP datagrams emitted by `/usr/bin/natpmpc -g 127.0.0.1 -a 1 0 udp 60`
    and compares them byte-for-byte with `encode_public_ip_request()` and `encode_mapping_request()`."""
    captured: list[str] = []
    ready = threading.Event()

    def _mock_gateway() -> None:
        srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 5351))
        srv.settimeout(3.0)
        ready.set()
        try:
            for _ in range(2):
                pkt, addr = srv.recvfrom(512)
                captured.append(pkt.hex())
                if len(pkt) == 2 and pkt == b"\x00\x00":
                    resp = struct.pack(
                        "!BBHII", 0, 128, 0, 42, struct.unpack("!I", socket.inet_aton("185.159.157.10"))[0]
                    )
                    srv.sendto(resp, addr)
                elif len(pkt) == 12:
                    ver, op, _r, iport, _sport, life = struct.unpack("!BBHHHI", pkt)
                    resp = struct.pack("!BBHIHHI", ver, 128 + op, 0, 43, iport, 51888, life)
                    srv.sendto(resp, addr)
        finally:
            srv.close()

    t = threading.Thread(target=_mock_gateway, daemon=True)
    t.start()
    ready.wait(timeout=2.0)

    proc = subprocess.run(
        ["natpmpc", "-g", "127.0.0.1", "-a", "1", "0", "udp", "60"],
        capture_output=True,
        text=True,
        timeout=5.0,
        check=False,
    )
    t.join(timeout=2.0)

    py_op0_hex = encode_public_ip_request().hex()
    # Rev. 3 plan assumed (internal_port=1, suggested_external_port=0), whereas `natpmpc -a <pub> <priv>`
    # puts argv[1]=1 in suggested_external_port (`publicport`) and argv[2]=0 in internal_port (`privateport`)!
    py_udp_rev3_assumed_hex = encode_mapping_request(OP_MAP_UDP, internal_port=1, suggested_external_port=0, lifetime_seconds=60).hex()
    py_udp_natpmpc_actual_hex = encode_mapping_request(OP_MAP_UDP, internal_port=0, suggested_external_port=1, lifetime_seconds=60).hex()

    # Negative validation checks: privileged port < 1024 and result_code = 3 (Unsupported)
    priv_port_rejected = False
    try:
        bad_pkt = struct.pack("!BBHIHHI", 0, 129, 0, 100, 1, 80, 60)
        decode_mapping_response(bad_pkt, OP_MAP_UDP)
    except NatPmpProtocolError:
        priv_port_rejected = True

    unsupported_rejected = False
    try:
        err_pkt = struct.pack("!BBHIHHI", 0, 129, 3, 100, 0, 0, 0)
        decode_mapping_response(err_pkt, OP_MAP_UDP)
    except NatPmpProtocolError:
        unsupported_rejected = True

    return {
        "natpmpc_returncode": proc.returncode,
        "natpmpc_captured_datagrams_hex": captured,
        "python_opcode0_hex": py_op0_hex,
        "python_rev3_assumed_iport1_sport0_hex": py_udp_rev3_assumed_hex,
        "python_natpmpc_actual_iport0_sport1_hex": py_udp_natpmpc_actual_hex,
        "rev3_assumed_matches_natpmpc": captured == [py_op0_hex, py_udp_rev3_assumed_hex],
        "corrected_iport0_sport1_matches_natpmpc": captured == [py_op0_hex, py_udp_natpmpc_actual_hex],
        "privileged_port_80_rejected": priv_port_rejected,
        "result_code_3_rejected": unsupported_rejected,
    }


def main() -> int:
    p2p_conf = os.environ.get("PROTON_P2P_WG_CONF", "").strip()
    non_p2p_conf = os.environ.get("PROTON_NON_P2P_WG_CONF", "").strip()
    offline_check = verify_wire_equivalence_with_natpmpc()

    if not p2p_conf or not Path(p2p_conf).is_file():
        print(
            json.dumps(
                {
                    "poc_id": "P0-4",
                    "gate_status": "NOT RUN",
                    "reason": (
                        "PROTON_P2P_WG_CONF is not configured in this test environment. "
                        "Per Phase 0 Rule 8, live Proton VPN P2P/non-P2P gateway behavior and "
                        "external inbound reachability are NEVER simulated."
                    ),
                    "has_non_p2p_conf": bool(non_p2p_conf and Path(non_p2p_conf).is_file()),
                    "offline_rfc6886_and_natpmpc_wire_equivalence": offline_check,
                },
                indent=2,
            )
        )
        return 0

    # Live Proton mode (when executed by operator on isolated Linux VM with real Proton WireGuard tunnel up)
    gateway = os.environ.get("PROTON_NATPMP_GATEWAY", "10.2.0.1")
    cycles = int(os.environ.get("PROTON_NATPMP_CYCLES", "10"))
    interval_s = float(os.environ.get("PROTON_NATPMP_INTERVAL", "45"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)

    sock.sendto(encode_public_ip_request(), (gateway, 5351))
    pub_raw, _ = sock.recvfrom(64)
    pub_info = decode_public_ip_response(pub_raw)

    timeline = []
    for cycle in range(1, cycles + 1):
        sock.sendto(encode_mapping_request(OP_MAP_UDP, 1, 0, 60), (gateway, 5351))
        u_raw, _ = sock.recvfrom(64)
        u_res = decode_mapping_response(u_raw, OP_MAP_UDP)

        sock.sendto(encode_mapping_request(OP_MAP_TCP, 1, 0, 60), (gateway, 5351))
        t_raw, _ = sock.recvfrom(64)
        t_res = decode_mapping_response(t_raw, OP_MAP_TCP)

        timeline.append(
            {
                "cycle": cycle,
                "udp_port": u_res["external_port"],
                "tcp_port": t_res["external_port"],
                "symmetric": u_res["external_port"] == t_res["external_port"],
                "udp_lifetime": u_res["lifetime"],
                "tcp_lifetime": t_res["lifetime"],
                "udp_resp_hex": u_raw.hex(),
                "tcp_resp_hex": t_raw.hex(),
            }
        )
        if cycle < cycles:
            time.sleep(interval_s)

    print(
        json.dumps(
            {
                "poc_id": "P0-4",
                "gate_status": "LIVE_GW_COMPLETED_PENDING_EXTERNAL_INBOUND_CHECK",
                "public_ip_response": pub_info,
                "renewal_timeline": timeline,
                "offline_rfc6886_and_natpmpc_wire_equivalence": offline_check,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

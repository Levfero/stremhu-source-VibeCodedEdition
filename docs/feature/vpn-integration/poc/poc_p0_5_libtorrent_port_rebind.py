#!/usr/bin/env python3
"""Phase 0 PoC P0-5: Libtorrent 2.1.1.0 Live `listen_interfaces` Port-Rebind PoC.

- Runs completely air-gapped (`--rm --network none --cap-add NET_ADMIN --cap-add SYS_ADMIN`, NO `--privileged`).
- Creates an isolated two-namespace topology (`seeder_ns` 10.2.0.1 <-> `worker_ns` 10.2.0.2 over `stremhu-wg0`
  kernel WireGuard tunnel on top of an internal veth link), with `UID=10001, Cap*=0, NoNewPrivs=1` privilege
  drop in the downloader Worker.
- Serves a deterministic 64 MiB (`16 x 4 MiB` pieces) payload at `8 MiB/s` upload rate limit (~8s transfer).
- At `piece #8` (32 MiB = 50% progress), dynamically updates `@bt_listen_port` (`6881 -> 54321`) and calls
  `session.apply_settings({"listen_interfaces": "10.2.0.2:54321"})`.
- Captures:
  1. Timestamped `listen_succeeded_alert` / `listen_failed_alert` / `peer_disconnected_alert` alerts + rebind latency (ms).
  2. `ss -tuln` output inside `worker_ns` before and after port rebind (confirming `:6881` closed, `:54321` bound,
     and zero wildcard `0.0.0.0` / `[::]` listeners).
  3. Both outbound-initiated and inbound-accepted peer behavior across live port rebind.
  4. Full 64 MiB SHA-256 integrity verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from poc_p0_1_netns_privdrop_and_helper import drop_all_privileges_and_verify  # noqa: E402

PIECE_SIZE = 4 * 1024 * 1024  # 4 MiB
NUM_PIECES = 16  # 64 MiB total
TOTAL_BYTES = PIECE_SIZE * NUM_PIECES


def generate_deterministic_payload(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("wb") as f:
        for idx in range(NUM_PIECES):
            # Deterministic non-compressible 4 MiB block derived from blake2b stream
            block = bytearray(PIECE_SIZE)
            seed = hashlib.blake2b(f"stremhu-p0-5-piece-{idx}".encode(), digest_size=64).digest()
            # Tile 64B blocks with index variation fast in C
            pattern = b"".join(
                hashlib.blake2b(seed + sub.to_bytes(2, "big"), digest_size=64).digest() for sub in range(1024)
            )  # 65,536 bytes
            chunk_4m = pattern * (PIECE_SIZE // len(pattern))
            f.write(chunk_4m)
            h.update(chunk_4m)
    return h.hexdigest()


def seeder_main(ctrl_fd: int, data_dir: str, torrent_path: str, connect_inbound_to_worker: bool) -> int:
    import libtorrent as lt  # noqa: PLC0415

    ctrl = socket.socket(fileno=ctrl_fd)
    f_in = ctrl.makefile("r")
    cmd = f_in.readline().strip()
    if cmd != "start_seeder":
        return 1

    sess = lt.session(
        {
            "listen_interfaces": "10.2.0.1:6889",
            "outgoing_interfaces": "wg-seed0",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "upload_rate_limit": 8 * 1024 * 1024,  # 8 MiB/s
            "alert_mask": lt.alert.category_t.all_categories,
        }
    )
    ti = lt.torrent_info(torrent_path)
    h = sess.add_torrent({"ti": ti, "save_path": data_dir, "flags": lt.torrent_flags.seed_mode})
    ctrl.sendall(b"seeder_ready\n")

    while True:
        line = f_in.readline().strip()
        if not line:
            break
        if line == "connect_inbound_to_worker":
            h.connect_peer(("10.2.0.2", 6881))
            ctrl.sendall(b"connect_peer_called\n")
        elif line == "stop":
            break
    del sess
    return 0


def downloader_worker_main(ctrl_fd: int, out_dir: str, torrent_path: str, mode: str) -> int:
    inbound_mode = mode == "inbound_tcp"
    mitigate_utp_freeze = mode == "outbound_mitigated"
    ctrl = socket.socket(fileno=ctrl_fd)
    f_in = ctrl.makefile("r")
    ppid = os.getppid()

    os.unshare(os.CLONE_NEWNET)
    ns_ino = os.stat("/proc/self/ns/net").st_ino
    ctrl.sendall((json.dumps({"stage": "unshared", "netns_ino": ns_ino}) + "\n").encode())

    if f_in.readline().strip() != "drop_and_start":
        return 90

    if sorted(n for _i, n in socket.if_nameindex()) != ["lo", "stremhu-wg0"]:
        os._exit(78)

    priv_audit = drop_all_privileges_and_verify(10001, 10001, ppid)
    import libtorrent as lt  # noqa: PLC0415

    t0 = time.monotonic()
    alerts_log: list[dict[str, object]] = []

    sess = lt.session(
        {
            "listen_interfaces": "10.2.0.2:6881",
            "outgoing_interfaces": "stremhu-wg0",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_upnp": False,
            "enable_natpmp": False,
            "enable_outgoing_utp": not mitigate_utp_freeze,
            "min_reconnect_time": 1,
            "alert_mask": lt.alert.category_t.all_categories,
        }
    )

    def drain_alerts(phase: str) -> list[object]:
        raw = sess.pop_alerts()
        for a in raw:
            cls_name = type(a).__name__
            if cls_name in (
                "listen_succeeded_alert",
                "listen_failed_alert",
                "peer_connect_alert",
                "peer_disconnected_alert",
                "torrent_finished_alert",
            ):
                alerts_log.append(
                    {
                        "t_ms": round((time.monotonic() - t0) * 1000, 2),
                        "phase": phase,
                        "type": cls_name,
                        "message": a.message(),
                    }
                )
        return raw

    # Wait for initial listen_succeeded_alert on 10.2.0.2:6881
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        sess.wait_for_alert(100)
        drain_alerts("init_listen")
        if any(x["type"] == "listen_succeeded_alert" for x in alerts_log):
            break

    ss_before = subprocess.check_output(["ss", "-tuln"], text=True).strip().splitlines()
    ti = lt.torrent_info(torrent_path)
    h = sess.add_torrent({"ti": ti, "save_path": out_dir})

    ctrl.sendall((json.dumps({"stage": "listening_6881", "ss_before": ss_before}) + "\n").encode())
    start_cmd = f_in.readline().strip()
    if start_cmd == "connect_outbound_to_seeder":
        h.connect_peer(("10.2.0.1", 6889))

    pieces_done = set()
    rebind_done = False
    rebind_latency_ms = None
    ss_after: list[str] = []
    finished = False

    # Bound timeout to 10s after rebind so bare uTP silent stall is measured cleanly without wasting 30s
    loop_deadline = time.monotonic() + 25.0
    rebind_wall_ts: float | None = None
    reconnected_triggered = False
    while time.monotonic() < loop_deadline and not finished:
        if rebind_wall_ts is not None and (time.monotonic() - rebind_wall_ts) > 8.0 and not finished:
            break
        sess.wait_for_alert(100)
        for a in drain_alerts("post_rebind" if rebind_done else "pre_rebind"):
            if isinstance(a, lt.piece_finished_alert):
                pieces_done.add(a.piece_index)
            elif isinstance(a, lt.torrent_finished_alert):
                finished = True
            elif rebind_done and isinstance(a, lt.peer_disconnected_alert) and not inbound_mode and not reconnected_triggered:
                # In real swarms, tracker peers stay in the peer list and reconnect after `min_reconnect_time`;
                # for a manual `connect_peer` test peer without tracker, re-queue `connect_peer` on uTP drop to measure recovery latency!
                reconnected_triggered = True
                h.connect_peer(("10.2.0.1", 6889))

        if not rebind_done and len(pieces_done) >= 8:
            # Notify parent to update @bt_listen_port -> 54321 first, then apply_settings!
            ctrl.sendall((json.dumps({"stage": "reached_piece_8", "pieces_count": len(pieces_done)}) + "\n").encode())
            if f_in.readline().strip() == "rebind_54321_now":
                t_rebind_start = time.monotonic()
                sess.apply_settings({"listen_interfaces": "10.2.0.2:54321"})
                # Wait for listen_succeeded_alert on 54321 and observe any immediate uTP/TCP peer_disconnected_alert
                wait_end = time.monotonic() + 2.0
                while time.monotonic() < wait_end:
                    sess.wait_for_alert(20)
                    for da in drain_alerts("during_rebind"):
                        if isinstance(da, lt.piece_finished_alert):
                            pieces_done.add(da.piece_index)
                        elif isinstance(da, lt.torrent_finished_alert):
                            finished = True
                    if any("54321" in str(x["message"]) and x["type"] == "listen_succeeded_alert" for x in alerts_log):
                        rebind_latency_ms = round((time.monotonic() - t_rebind_start) * 1000, 2)
                        break
                # Drain one short tick to catch any synchronous socket-close peer_disconnected_alert
                sess.wait_for_alert(100)
                for da in drain_alerts("during_rebind"):
                    if isinstance(da, lt.piece_finished_alert):
                        pieces_done.add(da.piece_index)
                    elif isinstance(da, lt.torrent_finished_alert):
                        finished = True
                if (
                    not inbound_mode
                    and not finished
                    and any(x["type"] == "peer_disconnected_alert" for x in alerts_log)
                ):
                    reconnected_triggered = True
                    h.connect_peer(("10.2.0.1", 6889))
                ss_after = subprocess.check_output(["ss", "-tuln"], text=True).strip().splitlines()
                rebind_done = True
                rebind_wall_ts = time.monotonic()

    # Ensure disk flush before computing SHA-256
    h.flush_cache()
    time.sleep(0.3)
    drain_alerts("final")
    del sess

    out_file = Path(out_dir) / "payload_64m.bin"
    sha256_actual = hashlib.sha256(out_file.read_bytes()).hexdigest() if out_file.exists() else "MISSING"

    peer_disconnects = [x for x in alerts_log if x["type"] == "peer_disconnected_alert"]
    listen_failures = [x for x in alerts_log if x["type"] == "listen_failed_alert"]

    ctrl.sendall(
        (
            json.dumps(
                {
                    "stage": "completed",
                    "inbound_mode": inbound_mode,
                    "priv_uid": priv_audit["status"]["Uid"],
                    "priv_cap_eff": priv_audit["status"]["CapEff"],
                    "pieces_downloaded": len(pieces_done),
                    "finished": finished,
                    "rebind_latency_ms": rebind_latency_ms,
                    "listen_failed_count": len(listen_failures),
                    "peer_disconnected_during_transfer_count": len(
                        [x for x in peer_disconnects if x["phase"] in ("during_rebind", "post_rebind") and "torrent finished" not in str(x["message"]).lower() and "upload to upload" not in str(x["message"]).lower()]
                    ),
                    "ss_before": ss_before,
                    "ss_after": ss_after,
                    "alerts_log": alerts_log,
                    "sha256": sha256_actual,
                }
            )
            + "\n"
        ).encode()
    )
    return 0


def run_single_scenario(
    torrent_path: Path,
    seed_dir: Path,
    expected_sha256: str,
    mode: str,
) -> dict[str, object]:
    inbound_mode = mode == "inbound_tcp"
    # Generate WireGuard keys for seeder (10.2.0.1) <-> worker (10.2.0.2) over veth (172.30.0.1 <-> 172.30.0.2)
    seed_priv = subprocess.check_output(["wg", "genkey"], text=True).strip()
    seed_pub = subprocess.check_output(["wg", "pubkey"], input=seed_priv + "\n", text=True).strip()
    work_priv = subprocess.check_output(["wg", "genkey"], text=True).strip()
    work_pub = subprocess.check_output(["wg", "pubkey"], input=work_priv + "\n", text=True).strip()

    subprocess.run(["ip", "link", "set", "lo", "up"], check=True)

    with tempfile.TemporaryDirectory(prefix="p0_5_dl_") as dl_dir:
        os.chmod(dl_dir, 0o777)

        # Spawn Worker subprocess (unshares CLONE_NEWNET)
        w_parent, w_child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        os.set_inheritable(w_child.fileno(), True)
        w_proc = subprocess.Popen(
            [
                sys.executable,
                __file__,
                "--downloader",
                str(w_child.fileno()),
                dl_dir,
                str(torrent_path),
                mode,
            ],
            pass_fds=(w_child.fileno(),),
        )
        w_child.close()
        wf = w_parent.makefile("r")
        _ = json.loads(wf.readline())
        w_ns_fd = os.open(f"/proc/{w_proc.pid}/ns/net", os.O_RDONLY | os.O_CLOEXEC)

        # Configure veth + Birthplace WireGuard: init_net acts as underlay + seeder on wg-seed0 (10.2.0.1)
        subprocess.run(["ip", "link", "del", "wg-seed0"], stderr=subprocess.DEVNULL, check=False)
        subprocess.run(["ip", "link", "add", "dev", "wg-seed0", "type", "wireguard"], check=True)
        subprocess.run(["ip", "addr", "add", "10.2.0.1/32", "dev", "wg-seed0"], check=True)
        seed_wg_conf = (
            f"[Interface]\nPrivateKey = {seed_priv}\nListenPort = 51821\n\n"
            f"[Peer]\nPublicKey = {work_pub}\nAllowedIPs = 10.2.0.2/32\n"
        )
        subprocess.run(["wg", "setconf", "wg-seed0", "/dev/stdin"], input=seed_wg_conf, text=True, check=True)
        subprocess.run(["ip", "link", "set", "mtu", "1420", "up", "dev", "wg-seed0"], check=True)
        subprocess.run(["ip", "route", "replace", "10.2.0.2/32", "dev", "wg-seed0"], check=True)

        # Create stremhu-wg0 in init_net (Birthplace = init_net where 127.0.0.1:51821 is reachable!) and move to worker_ns!
        subprocess.run(["ip", "link", "add", "dev", "stremhu-wg0", "type", "wireguard"], check=True)
        os.set_inheritable(w_ns_fd, True)
        subprocess.run(
            ["ip", "link", "set", "dev", "stremhu-wg0", "netns", f"/proc/self/fd/{w_ns_fd}"],
            pass_fds=(w_ns_fd,),
            check=True,
        )

        # Configure stremhu-wg0 + Kill Switch @bt_listen_port inside worker_ns
        # Note: PersistentKeepalive = 25 initiates the WireGuard handshake immediately so inbound peer connections work!
        work_wg_conf = (
            f"[Interface]\nPrivateKey = {work_priv}\n\n"
            f"[Peer]\nPublicKey = {seed_pub}\nEndpoint = 127.0.0.1:51821\nAllowedIPs = 0.0.0.0/0\nPersistentKeepalive = 25\n"
        )
        nft_rules = """
table inet stremhu_killswitch
delete table inet stremhu_killswitch
table inet stremhu_killswitch {
    set bt_listen_port { type inet_service; elements = { 6881 }; }
    chain input {
        type filter hook input priority filter; policy drop;
        iifname "lo" accept
        ct state established,related accept
        iifname "stremhu-wg0" tcp dport @bt_listen_port accept
        iifname "stremhu-wg0" udp dport @bt_listen_port accept
    }
    chain output {
        type filter hook output priority filter; policy drop;
        oifname "lo" accept
        oifname "stremhu-wg0" meta nfproto ipv4 accept
    }
}
"""
        helper_code = (
            f"import os, subprocess; os.setns({w_ns_fd}, os.CLONE_NEWNET); "
            "subprocess.run(['ip','link','set','lo','up'], check=True); "
            "subprocess.run(['ip','addr','add','10.2.0.2/32','dev','stremhu-wg0'], check=True); "
            f"subprocess.run(['wg','setconf','stremhu-wg0','/dev/stdin'], input={work_wg_conf!r}, text=True, check=True); "
            "subprocess.run(['ip','link','set','mtu','1420','up','dev','stremhu-wg0'], check=True); "
            "subprocess.run(['ip','route','add','default','dev','stremhu-wg0'], check=True); "
            f"subprocess.run(['nft','-f','-'], input={nft_rules!r}, text=True, check=True)"
        )
        subprocess.run([sys.executable, "-c", helper_code], pass_fds=(w_ns_fd,), check=True)
        os.set_inheritable(w_ns_fd, False)

        # Start seeder subprocess
        s_parent, s_child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        os.set_inheritable(s_child.fileno(), True)
        s_proc = subprocess.Popen(
            [
                sys.executable,
                __file__,
                "--seeder",
                str(s_child.fileno()),
                str(seed_dir),
                str(torrent_path),
                "1" if inbound_mode else "0",
            ],
            pass_fds=(s_child.fileno(),),
        )
        s_child.close()
        sf = s_parent.makefile("r")
        s_parent.sendall(b"start_seeder\n")
        assert sf.readline().strip() == "seeder_ready"

        # Tell Worker to drop privileges and bind :6881
        w_parent.sendall(b"drop_and_start\n")
        _ = json.loads(wf.readline())

        if inbound_mode:
            w_parent.sendall(b"wait_for_inbound_seeder\n")
            s_parent.sendall(b"connect_inbound_to_worker\n")
            assert sf.readline().strip() == "connect_peer_called"
        else:
            w_parent.sendall(b"connect_outbound_to_seeder\n")

        mid_msg = json.loads(wf.readline())
        assert mid_msg["stage"] == "reached_piece_8", f"Unexpected msg: {mid_msg}"

        # Atomically update @bt_listen_port { 6881 -> 54321 } in worker_ns, then trigger live apply_settings!
        os.set_inheritable(w_ns_fd, True)
        upd_nft = (
            "flush set inet stremhu_killswitch bt_listen_port\n"
            "add element inet stremhu_killswitch bt_listen_port { 54321 }\n"
        )
        subprocess.run(
            [
                sys.executable,
                "-c",
                f"import os, subprocess; os.setns({w_ns_fd}, os.CLONE_NEWNET); subprocess.run(['nft','-f','-'], input={upd_nft!r}, text=True, check=True)",
            ],
            pass_fds=(w_ns_fd,),
            check=True,
        )
        os.set_inheritable(w_ns_fd, False)

        w_parent.sendall(b"rebind_54321_now\n")
        final_msg = json.loads(wf.readline())

        s_parent.sendall(b"stop\n")
        s_parent.close()
        w_parent.close()
        s_proc.wait(timeout=5)
        w_proc.wait(timeout=5)
        os.close(w_ns_fd)
        subprocess.run(["ip", "link", "del", "wg-seed0"], stderr=subprocess.DEVNULL, check=False)

        final_msg["expected_sha256"] = expected_sha256
        final_msg["sha256_match"] = final_msg["sha256"] == expected_sha256
        return final_msg


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--seeder":
        return seeder_main(int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5] == "1")
    if len(sys.argv) >= 2 and sys.argv[1] == "--downloader":
        return downloader_worker_main(int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5])

    import libtorrent as lt  # noqa: PLC0415

    with tempfile.TemporaryDirectory(prefix="p0_5_seed_") as seed_tmp:
        seed_dir = Path(seed_tmp)
        os.chmod(seed_dir, 0o755)
        payload_file = seed_dir / "payload_64m.bin"
        expected_sha256 = generate_deterministic_payload(payload_file)

        fs = lt.file_storage()
        lt.add_files(fs, str(payload_file))
        ct = lt.create_torrent(fs, PIECE_SIZE)
        lt.set_piece_hashes(ct, str(seed_dir))
        torrent_path = seed_dir / "payload_64m.torrent"
        torrent_path.write_bytes(lt.bencode(ct.generate()))

        outbound_utp_res = run_single_scenario(torrent_path, seed_dir, expected_sha256, mode="outbound_utp_default")
        inbound_tcp_res = run_single_scenario(torrent_path, seed_dir, expected_sha256, mode="inbound_tcp")
        outbound_mitigated_res = run_single_scenario(torrent_path, seed_dir, expected_sha256, mode="outbound_mitigated")

        print(
            json.dumps(
                {
                    "poc_id": "P0-5",
                    "libtorrent_version": lt.__version__,
                    "payload_bytes": TOTAL_BYTES,
                    "piece_size_bytes": PIECE_SIZE,
                    "num_pieces": NUM_PIECES,
                    "outbound_utp_default_rebind_test": outbound_utp_res,
                    "inbound_tcp_rebind_test": inbound_tcp_res,
                    "outbound_tcp_only_mitigated_rebind_test": outbound_mitigated_res,
                },
                indent=2,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

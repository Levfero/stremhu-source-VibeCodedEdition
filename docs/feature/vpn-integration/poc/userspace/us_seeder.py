#!/usr/bin/env python3
"""Userspace-VPN PoC — swarm seeder (test infrastructure; the "internet" side).

Creates a deterministic test file and torrent (/share/test.torrent, trackers: HTTP + UDP on
tracker.poc.test), then seeds it on 0.0.0.0:6881 with TCP and uTP. Every peer it sees is logged
with its address and transport to /share/seeder_peers.json, so the orchestrator can prove that the
app reached it only through the VPN exit (172.30.20.2), never from the app's direct IP.
Control file /share/seeder_ctl.json: {"seq": n, "upload_rate": bytes_per_s, "connect": [ip, port]}.
"""

import hashlib
import json
import os
import random
import sys
import time
from pathlib import Path

import libtorrent as lt

SHARE = Path("/share")
DATA = Path("/data")
SIZE_MIB = int(os.environ.get("POC_SIZE_MIB", "256"))
PIECE = 4 * 1024 * 1024
UTP = getattr(lt.peer_info, "utp_socket", 1 << 17)


def create() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    path = DATA / "poc-video.mkv"
    rng = random.Random(20261108)
    digest = hashlib.sha256()
    with path.open("wb") as f:
        for _ in range(SIZE_MIB):
            block = rng.randbytes(1024 * 1024)
            digest.update(block)
            f.write(block)
    fs = lt.file_storage()
    lt.add_files(fs, str(path))
    t = lt.create_torrent(fs, PIECE)
    t.add_tracker("http://tracker.poc.test:6969/announce", 0)
    t.add_tracker("udp://tracker.poc.test:6969/announce", 1)
    lt.set_piece_hashes(t, str(DATA))
    (SHARE / "test.torrent").write_bytes(lt.bencode(t.generate()))
    (SHARE / "test.sha256").write_text(digest.hexdigest())


def main() -> None:
    create()
    ses = lt.session({
        "listen_interfaces": "0.0.0.0:6881",
        "enable_dht": False, "enable_lsd": False, "enable_upnp": False, "enable_natpmp": False,
        "enable_outgoing_utp": False, "enable_incoming_utp": True,  # connect_peer() probes must use TCP
        "allow_multiple_connections_per_ip": True,
        "alert_mask": lt.alert_category.error | lt.alert_category.peer | lt.alert_category.status,
    })
    atp = lt.load_torrent_file(str(SHARE / "test.torrent"))
    atp.save_path = str(DATA)
    atp.flags |= lt.torrent_flags.seed_mode
    handle = ses.add_torrent(atp)
    seen: dict[str, dict] = {}
    last_seq = -1
    (SHARE / "seeder_ready").write_text("ok")
    while True:
        ctl_path = SHARE / "seeder_ctl.json"
        if ctl_path.exists():
            try:
                ctl = json.loads(ctl_path.read_text())
            except ValueError:
                ctl = {}
            if ctl.get("seq", -1) != last_seq:
                last_seq = ctl["seq"]
                handle.set_upload_limit(int(ctl.get("upload_rate", 0)) or -1)
                ses.apply_settings({"enable_outgoing_utp": bool(ctl.get("outgoing_utp", False))})
                if ctl.get("connect"):
                    ip, port = ctl["connect"]
                    handle.connect_peer((ip, int(port)))
                if ctl.get("reannounce"):
                    handle.force_reannounce()
        for alert in ses.pop_alerts():
            if isinstance(alert, lt.incoming_connection_alert):
                key = f"incoming {alert.endpoint[0]}"
                seen.setdefault(key, {"first": time.time(), "count": 0})["count"] += 1
        for p in handle.get_peer_info():
            transport = "utp" if p.flags & UTP else "tcp"
            key = f"peer {p.ip[0]} {transport}"
            entry = seen.setdefault(key, {"first": time.time(), "count": 0, "bytes_up": 0})
            entry["bytes_up"] = max(entry.get("bytes_up", 0), p.total_upload)
        (SHARE / "seeder_peers.json.tmp").write_text(json.dumps(seen))
        (SHARE / "seeder_peers.json.tmp").replace(SHARE / "seeder_peers.json")
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())

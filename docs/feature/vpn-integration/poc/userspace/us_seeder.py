#!/usr/bin/env python3
"""Userspace-VPN PoC — swarm seeder (test infrastructure; the "internet" side).

Creates a deterministic test file and torrent (/share/test.torrent, trackers: HTTP + UDP on
tracker.poc.test), then seeds it on 0.0.0.0:6881 with TCP and uTP. Every peer it sees is logged
with its address and transport to /share/seeder_peers.json, so the orchestrator can prove that the
app reached it only through the VPN exit (172.30.20.2), never from the app's direct IP.
G4: the same file is also served as an HTTP web seed (Range/206) on 0.0.0.0:8080/poc-video.mkv; every
request is logged as "http <source>" with its served bytes in the same file.
Control file /share/seeder_ctl.json: {"seq": n, "upload_rate": bytes_per_s, "connect": [ip, port]}.
"""

import hashlib
import json
import os
import random
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import libtorrent as lt

SHARE = Path("/share")
DATA = Path("/data")
SIZE_MIB = int(os.environ.get("POC_SIZE_MIB", "256"))
PIECE = 4 * 1024 * 1024
UTP = getattr(lt.peer_info, "utp_socket", 1 << 17)
VIDEO = DATA / "poc-video.mkv"
HTTP_LOCK = threading.Lock()
HTTP_SEEN: dict[str, dict] = {}


class WebSeed(BaseHTTPRequestHandler):
    """Minimal BEP 19 web seed: GET/HEAD of the single file with one byte range."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def do_HEAD(self):
        self._serve(head=True)

    def do_GET(self):
        self._serve(head=False)

    def _serve(self, head: bool) -> None:
        if self.path.split("?", 1)[0] != "/poc-video.mkv":
            self.send_error(404)
            return
        size = VIDEO.stat().st_size
        start, end, status = 0, size - 1, 200
        header = self.headers.get("Range")
        if header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
            if not match or not (match.group(1) or match.group(2)):
                self.send_error(416)
                return
            if match.group(1):
                start = int(match.group(1))
                end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
            else:
                start = max(0, size - int(match.group(2)))
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        sent = 0
        if not head:
            with VIDEO.open("rb") as f:
                f.seek(start)
                while sent < length:
                    chunk = f.read(min(length - sent, 1 << 20))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
        with HTTP_LOCK:
            entry = HTTP_SEEN.setdefault(f"http {self.client_address[0]}", {"first": time.time(), "count": 0, "bytes_up": 0})
            entry["count"] += 1
            entry["bytes_up"] += sent


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
    threading.Thread(target=ThreadingHTTPServer(("0.0.0.0", 8080), WebSeed).serve_forever, daemon=True).start()
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
        with HTTP_LOCK:
            snapshot = {**seen, **{k: dict(v) for k, v in HTTP_SEEN.items()}}
        (SHARE / "seeder_peers.json.tmp").write_text(json.dumps(snapshot))
        (SHARE / "seeder_peers.json.tmp").replace(SHARE / "seeder_peers.json")
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())

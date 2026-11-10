"""G0 / SP10 (stream-performance): stalled peers versus deadline (time-critical) pieces.

Functional probe, not a gate measurement. Runs in a network-disabled container. The
synthetic swarm runs in child processes (sp_seed_service.py for the serving peers; a
small built-in seed for the stalled peers, "silent" ones and "chatty" ones that keep
sending keep-alives, each with a distinct peer-id). Every stalled peer has its own loopback address, so an IP-level
action can target exactly one peer. Piece completion comes from piece_finished alerts;
block ownership from one get_download_queue() per sample.

Per run: a fresh libtorrent session with the relay's session settings
(relay/service.py) plus one variant override, one torrent (rest at priority 0), the
serving swarm and the variant's stalled peers. Scenarios (deadlines):
  window3  the product's window on pieces 0/1/2 (0 / 300 / 1000+500*2 ms), set once;
  legacy   relay start policy "legacy" + readahead "legacy" (head and tail 3 pieces at
           0 ms, 4-piece window), re-applied on every finished piece like
           Torrent.priority_manager (gsp02-v4 C0);
  edges    start policy "edges" + readahead "legacy" (gsp02-v4 C4), re-applied likewise;
  upstream start policy "edges" + readahead "upstream" (gsp02-v4 C2, the default since
           5c14f1e): first and last piece at 0 ms, window 0 / 2000+1000*d ms, re-applied likewise.
Measured: seconds from the first connect_peer until pieces 0/1/2 and the last piece
complete; per stalled peer the libtorrent reactions (snub, block time-outs, disconnect,
queued requests) and how many blocks of the first pieces waited on a stalled peer only.

Usage (inside the image):
  python probe_sp_stalled_peers.py --scenario legacy --swarm mixed --variants ctrl,product --reps 3
  python probe_sp_stalled_peers.py --list
Output: one JSON document on stdout; progress on stderr.
"""

import argparse
import asyncio
import functools
import json
import signal
import struct
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "fixtures"))
from synthetic_torrent import build  # noqa: E402

MIB = 1024 * 1024
PIECES = {1: 256, 4: 64, 16: 32}  # 256 / 256 / 512 MiB synthetic files
SWARMS = {
    # 4 equal serving peers.
    "fast4": [{"rate_bytes_per_s": 2_500_000, "latency_s": 0.03, "jitter_s": 0.01}] * 4,
    # pb0-v2 "mixed" without its two stalled peers (those come from the variant).
    "mixed": [{"rate_bytes_per_s": 3_000_000, "latency_s": 0.03, "jitter_s": 0.01}] * 3
    + [{"rate_bytes_per_s": 500_000, "latency_s": 0.1, "jitter_s": 0.05}] * 3,
}
SERVING_HOST = "127.0.0.1"
SILENT_HOSTS = ("127.0.0.2", "127.0.0.3")
CHATTY_HOSTS = ("127.0.0.4", "127.0.0.5")
CHATTY_KEEPALIVE_S = 1.0
WINDOW3 = ((0, 0), (1, 300), (2, 1000 + 500 * 2))  # (piece, deadline ms)
SAMPLE_S = 0.25
BLOCK_REQUESTED = 1

# relay/service.py session settings (picker/timeout relevant ones identical to the product).
PRODUCT_SETTINGS = {
    "connections_limit": 300, "auto_sequential": False, "piece_extent_affinity": True,
    "peer_timeout": 60, "piece_timeout": 10, "request_timeout": 30, "unchoke_interval": 10,
    "active_downloads": -1, "active_seeds": -1, "active_limit": -1, "connection_speed": 100,
    "unchoke_slots_limit": 16, "min_reconnect_time": 5, "strict_end_game_mode": False,
    "optimistic_unchoke_interval": 20,
}

# Settings overrides on top of PRODUCT_SETTINGS. Each set runs with silent stalled peers (bare
# name), with chatty stalled peers ("<name>_chatty") and without stalled peers ("ctrl_<name>").
SETTING_SETS: dict[str, dict] = {
    "product": {},
    "req_t5": {"request_timeout": 5},
    "req_t2": {"request_timeout": 2},
    "piece_t5": {"piece_timeout": 5},
    "piece_t3": {"piece_timeout": 3},
    "piece_t2": {"piece_timeout": 2},
    "peer_t5": {"peer_timeout": 5},
    "strict_eg": {"strict_end_game_mode": True},
    "no_affinity": {"piece_extent_affinity": False},
    "outq8": {"max_out_request_queue": 8},
    "rqtime1": {"request_queue_time": 1},
    "piece_t3_req_t5": {"piece_timeout": 3, "request_timeout": 5},
    "piece_t2_req_t2": {"piece_timeout": 2, "request_timeout": 2},
    # Freed deadline blocks wait while every serving peer has > 2 s queued (request_queue_time 3).
    "piece_t2_rqtime1": {"piece_timeout": 2, "request_queue_time": 1},
    "piece_t3_rqtime1": {"piece_timeout": 3, "request_queue_time": 1},
}
VARIANTS: dict[str, dict] = {}
for _name, _settings in SETTING_SETS.items():
    VARIANTS[_name] = {"stall": "silent", "settings": _settings}
    VARIANTS[f"{_name}_chatty"] = {"stall": "chatty", "settings": _settings}
    VARIANTS["ctrl" if _name == "product" else f"ctrl_{_name}"] = {"stall": None, "settings": _settings}
VARIANTS.update({
    "product_1stall": {"stall": "silent", "count": 1, "settings": {}},
    "stall_last": {"stall": "silent", "order": "stall_last", "settings": {},
                   "doc": "stalled peers connected 1 s after the serving ones"},
    "watchdog": {"stall": "silent", "watchdog": 2.0, "settings": {},
                 "doc": "probe-side detector: requests outstanding and no payload for 2 s -> ip_filter that address"},
    "watchdog_chatty": {"stall": "chatty", "watchdog": 2.0, "settings": {}},
    "ctrl_watchdog": {"stall": None, "watchdog": 2.0, "settings": {}},
})


# --------------------------------------------------------------------------- stalled seed child


class _UtpRefuserProtocol(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        if len(data) >= 20 and data[0] == 0x41:
            self.transport.sendto(bytes([0x31, 0]) + data[2:4] + bytes(12) + b"\0\0" + data[16:18], addr)


async def stall_main(spec_path: str) -> None:
    """Stalled peers: unchoke, ignore every request; "chatty" ones send a keep-alive each second.

    Own child instead of sp_seed_service: every connection gets a distinct peer-id (separate
    sp_seed_service processes repeat theirs, which libtorrent rejects as "duplicate peer-id"
    when allow_multiple_connections_per_ip is on).
    """
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    torrents = {}
    for item in spec["torrents"]:
        t = build(item["name"], item["piece_size"], item["total_size"])
        torrents[t.info_hash] = t.piece_count
    stats = {"handshakes": 0, "requests_ignored": 0, "keepalives_sent": 0}
    writers = set()
    sequence = iter(range(1, 10**9))

    async def serve(keepalive_s, octet, reader, writer):
        writers.add(writer)
        try:
            handshake = await asyncio.wait_for(reader.readexactly(68), 10)
            count = torrents[handshake[28:48]]
            bitfield = bytearray(b"\xff" * ((count + 7) // 8))
            if count % 8:
                bitfield[-1] = (255 << (8 - count % 8)) & 255
            peer_id = b"-CR0002-" + b"%03d%09d" % (octet, next(sequence))
            writer.write(b"\x13BitTorrent protocol" + b"\0" * 8 + handshake[28:48] + peer_id)
            writer.write(struct.pack("!I", len(bitfield) + 1) + b"\x05" + bitfield + b"\0\0\0\x01\x01")
            await writer.drain()
            stats["handshakes"] += 1

            async def keepalive():
                while True:
                    await asyncio.sleep(keepalive_s)
                    writer.write(b"\0\0\0\0")
                    await writer.drain()
                    stats["keepalives_sent"] += 1

            ka = asyncio.create_task(keepalive()) if keepalive_s else None
            try:
                while True:
                    size = struct.unpack("!I", await reader.readexactly(4))[0]
                    if size:
                        message = await reader.readexactly(size)
                        if message[0] == 6:
                            stats["requests_ignored"] += 1
            finally:
                if ka is not None:
                    ka.cancel()
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.CancelledError, asyncio.TimeoutError, KeyError):
            pass
        finally:
            writers.discard(writer)
            writer.close()

    loop = asyncio.get_running_loop()
    servers, udps, endpoints = [], [], []
    for item in spec["hosts"]:
        host = item["host"]
        server = await asyncio.start_server(
            functools.partial(serve, item["keepalive_s"], int(host.rsplit(".", 1)[1])), host, 0)
        endpoint = server.sockets[0].getsockname()[:2]
        transport, _ = await loop.create_datagram_endpoint(_UtpRefuserProtocol, local_addr=endpoint)
        servers.append(server)
        udps.append(transport)
        endpoints.append(endpoint)
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    print(json.dumps({"ready": True, "endpoints": endpoints}), flush=True)
    await stop.wait()
    Path(spec["stats"]).write_text(json.dumps(stats), encoding="utf-8")
    for transport in udps:
        transport.close()
    for writer in tuple(writers):
        writer.close()
    for server in servers:
        server.close()


# --------------------------------------------------------------------------- deadlines


def product_targets(scenario: str, have: list[bool], n: int) -> dict[int, int]:
    """relay/entities.py get_deadlines() for one stream (insertion order = native call order)."""
    targets: dict[int, int] = {}

    def offer(piece: int, deadline: int) -> None:
        if not have[piece] and (piece not in targets or deadline < targets[piece]):
            targets[piece] = deadline

    if scenario == "legacy":  # _set_file_boundary_priorities
        for i in range(3):
            offer(i, 0)
            offer(n - 1 - i, 0)
    elif scenario in ("edges", "upstream"):  # _set_edge_deadlines
        offer(0, 0)
        offer(n - 1, 0)
    # _set_legacy_stream_deadlines / _set_upstream_stream_deadlines; the client reads at once, so
    # the stream sits on the first missing piece.
    current = next((p for p in range(n) if not have[p]), n)
    fetched = 0
    for piece in range(current, n):
        if have[piece]:
            continue
        d = piece - current
        if scenario == "upstream":
            offer(piece, 0 if d == 0 else 2000 + 1000 * d)
        else:
            offer(piece, 0 if d == 0 else 300 if d == 1 else 1000 + 500 * d)
        fetched += 1
        if fetched >= 4:
            break
    return targets


# --------------------------------------------------------------------------- probe


def endpoint_of(alert) -> tuple[str, int] | None:
    ep = getattr(alert, "endpoint", None) or getattr(alert, "ip", None)
    if ep is None:
        return None
    return (str(ep[0]), int(ep[1]))


class Run:
    def __init__(self, lt, ses, stalled: dict[tuple[str, int], str], t0_ref: list[float]):
        self.lt = lt
        self.ses = ses
        self.stalled = stalled
        self.t0_ref = t0_ref
        self.done: dict[int, float] = {}
        self.new_finished = False
        self.peer: dict[tuple[str, int], dict] = {}
        self.block_timeouts_serving = 0
        self.serving_disconnects: list[str] = []
        self.serving_snubs: list[float] = []

    def rel(self) -> float:
        return round(time.monotonic() - self.t0_ref[0], 3)

    def stall_rec(self, ep) -> dict | None:
        if ep not in self.stalled:
            return None
        return self.peer.setdefault(ep, {
            "kind": self.stalled[ep], "first_request_s": None, "max_queue": 0, "snub_s": None,
            "first_block_timeout_s": None, "block_timeouts": 0, "block_timeouts_first_pieces": 0,
            "disconnects": [], "request_timeout_samples": []})

    def pump(self) -> None:
        lt = self.lt
        for alert in self.ses.pop_alerts():
            if isinstance(alert, lt.piece_finished_alert):
                if alert.piece_index not in self.done:
                    self.done[alert.piece_index] = self.rel()
                    self.new_finished = True
            elif isinstance(alert, lt.block_timeout_alert):
                rec = self.stall_rec(endpoint_of(alert))
                if rec is None:
                    self.block_timeouts_serving += 1
                    continue
                rec["block_timeouts"] += 1
                if alert.piece_index in (0, 1, 2):
                    rec["block_timeouts_first_pieces"] += 1
                if rec["first_block_timeout_s"] is None:
                    rec["first_block_timeout_s"] = self.rel()
            elif isinstance(alert, lt.peer_snubbed_alert):
                rec = self.stall_rec(endpoint_of(alert))
                if rec is None:
                    self.serving_snubs.append(self.rel())  # false positive: a serving peer was snubbed
                elif rec["snub_s"] is None:
                    rec["snub_s"] = self.rel()
            elif isinstance(alert, lt.peer_disconnected_alert):
                reason = alert.error.message() if alert.error else str(alert.reason)
                rec = self.stall_rec(endpoint_of(alert))
                if rec is None:
                    self.serving_disconnects.append(reason)
                else:
                    rec["disconnects"].append([self.rel(), str(alert.socket_type), reason])


def make_session(lt, overrides: dict):
    ses = lt.session()
    settings = dict(PRODUCT_SETTINGS)
    settings.update(overrides)
    settings.update({
        "listen_interfaces": "127.0.0.1:0", "enable_dht": False, "enable_lsd": False,
        "enable_upnp": False, "enable_natpmp": False, "allow_multiple_connections_per_ip": True,
        "mixed_mode_algorithm": int(lt.bandwidth_mixed_algo_t.prefer_tcp),
        "out_enc_policy": int(lt.enc_policy.disabled), "in_enc_policy": int(lt.enc_policy.disabled),
        "alert_queue_size": 100000,
        "alert_mask": int(lt.alert_category.error | lt.alert_category.storage | lt.alert_category.status
                          | lt.alert_category.piece_progress | lt.alert_category.peer
                          | lt.alert_category.connect | lt.alert_category.ip_block),
    })
    ses.apply_settings(settings)
    applied = ses.get_settings()
    return ses, {k: applied[k] for k in sorted(set(PRODUCT_SETTINGS) | set(overrides))}


async def run_once(lt, scenario: str, name: str, piece_mib: int, torrent, swarm: dict, cap_s: float) -> dict:
    variant = VARIANTS[name]
    ses, applied = make_session(lt, variant.get("settings", {}))
    kind = variant.get("stall")
    stalled_eps = [tuple(e) for e in swarm[kind][: variant.get("count", 2)]] if kind else []
    stalled = {ep: kind for ep in stalled_eps}
    n = torrent.piece_count
    watched = (0, 1, 2, n - 1) if scenario != "window3" else (0, 1, 2)
    with tempfile.TemporaryDirectory() as tmp:
        params = lt.add_torrent_params()
        params.ti = lt.torrent_info(torrent.torrent_bytes)
        params.save_path = tmp
        params.storage_mode = lt.storage_mode_t.storage_mode_sparse
        params.flags &= ~(lt.torrent_flags.auto_managed | lt.torrent_flags.paused)
        handle = ses.add_torrent(params)
        handle.prioritize_pieces([0] * n)
        handle.set_max_connections(50)  # relay: active stream -> max(limit, 50)
        active: dict[int, int] = {}
        updates = 0

        def apply_deadlines() -> None:
            nonlocal updates
            if scenario == "window3":
                targets = {p: d for p, d in WINDOW3 if p not in run.done}
            else:
                have = list(handle.status(lt.status_flags_t.query_pieces).pieces)
                targets = product_targets(scenario, have, n)
            for piece in list(active):
                if piece not in targets:
                    handle.reset_piece_deadline(piece)
                    active.pop(piece)
            for piece, deadline in targets.items():
                handle.set_piece_deadline(piece, deadline)
                active[piece] = deadline
            updates += 1

        t0_ref = [0.0]
        run = Run(lt, ses, stalled, t0_ref)
        apply_deadlines()
        serving = [tuple(e) for e in swarm["serving"]]
        t0_ref[0] = time.monotonic()
        if variant.get("order", "stall_first") == "stall_first":
            for ep in stalled_eps + serving:
                handle.connect_peer(ep)
            pending_stalled = []
        else:
            for ep in serving:
                handle.connect_peer(ep)
            pending_stalled = list(stalled_eps)
        watchdog_s = variant.get("watchdog")
        progress: dict[tuple[str, int], tuple[int, float]] = {}
        blocked_ips: list[str] = []
        blocked: list[dict] = []
        stuck = {p: {"max": 0, "last_s": None} for p in watched}
        next_sample = 0.0
        while True:
            now = time.monotonic() - t0_ref[0]
            run.pump()
            if run.new_finished:
                run.new_finished = False
                if scenario != "window3":
                    apply_deadlines()  # Torrent.priority_manager on piece_finished
            if pending_stalled and now >= 1.0:
                for ep in pending_stalled:
                    handle.connect_peer(ep)
                pending_stalled = []
            if all(p in run.done for p in watched) or now >= cap_s:
                break
            if now >= next_sample:
                next_sample = now + SAMPLE_S
                if stalled:
                    for entry in handle.get_download_queue():
                        piece = entry["piece_index"]
                        if piece not in stuck:
                            continue
                        waiting = sum(1 for b in entry["blocks"] if b["state"] == BLOCK_REQUESTED
                                      and b["num_peers"] == 1 and (str(b["peer"][0]), int(b["peer"][1])) in stalled)
                        if waiting:
                            stuck[piece]["max"] = max(stuck[piece]["max"], waiting)
                            stuck[piece]["last_s"] = round(now, 3)
                for info in handle.get_peer_info():
                    ep = (str(info.ip[0]), int(info.ip[1]))
                    rec = run.stall_rec(ep)
                    if rec is not None:
                        rec["max_queue"] = max(rec["max_queue"], info.download_queue_length)
                        if info.download_queue_length and rec["first_request_s"] is None:
                            rec["first_request_s"] = round(now, 3)
                        if len(rec["request_timeout_samples"]) < 3 and info.download_queue_length:
                            rec["request_timeout_samples"].append(info.request_timeout)
                    if watchdog_s is None:
                        continue
                    # Detector without ground truth: requests outstanding, no payload progress for N s.
                    last_bytes, since = progress.get(ep, (-1, now))
                    if info.total_download != last_bytes or not info.download_queue_length:
                        progress[ep] = (info.total_download, now)
                    elif now - since >= watchdog_s and ep[0] not in blocked_ips:
                        blocked_ips.append(ep[0])
                        flt = lt.ip_filter()
                        for ip in blocked_ips:
                            flt.add_rule(ip, ip, 1)
                        ses.set_ip_filter(flt)
                        blocked.append({"ip": ep[0], "at_s": round(now, 3), "was_stalled": ep in stalled,
                                        "queue": info.download_queue_length})
            await asyncio.sleep(0.01)
        status = handle.status()
        result = {
            "scenario": scenario, "variant": name, "piece_mib": piece_mib,
            "piece_s": {("last" if p == n - 1 else str(p)): run.done.get(p) for p in watched},
            "all_done": all(p in run.done for p in watched),
            "stuck_blocks": {("last" if p == n - 1 else str(p)): v for p, v in stuck.items()} if stalled else None,
            "stalled_peers": [run.peer.get(ep, {"kind": kind, "never_seen": True}) for ep in stalled_eps],
            "block_timeouts_serving_peers": run.block_timeouts_serving,
            "serving_disconnects_before_end": len(run.serving_disconnects),
            "serving_snubs_s": run.serving_snubs,
            "watchdog_blocks": blocked if watchdog_s is not None else None,
            "deadline_updates": updates,
            "payload_downloaded_mib": round(status.total_payload_download / MIB, 2),
            "redundant_mib": round(status.total_redundant_bytes / MIB, 2),
            "settings": applied,
        }
        ses.remove_torrent(handle)
        ses.pause()
    del ses
    return result


async def start_child(args: list[str]) -> tuple[asyncio.subprocess.Process, list]:
    proc = await asyncio.create_subprocess_exec(sys.executable, *args, stdout=asyncio.subprocess.PIPE)
    assert proc.stdout is not None
    ready = json.loads(await asyncio.wait_for(proc.stdout.readline(), 60))
    return proc, ready["endpoints"]


async def main(argv: list[str]) -> None:
    import libtorrent as lt

    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", choices=("window3", "legacy", "edges", "upstream"), default="window3")
    parser.add_argument("--swarm", choices=tuple(SWARMS), default="fast4")
    parser.add_argument("--variants", default="ctrl,product")
    parser.add_argument("--piece-mib", default="4,16")
    parser.add_argument("--reps", type=int, default=3)
    parser.add_argument("--cap-s", type=float, default=60.0)
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)
    if args.list:
        print(json.dumps(VARIANTS, indent=1))
        return
    names = [n for n in args.variants.split(",") if n]
    unknown = [n for n in names if n not in VARIANTS]
    if unknown:
        raise SystemExit(f"unknown variants: {unknown}")
    sizes = [int(s) for s in args.piece_mib.split(",")]
    torrents_spec = [{"id": f"p{s}", "name": f"sp10-p{s}.mkv", "piece_size": s * MIB,
                      "total_size": s * MIB * PIECES[s]} for s in sizes]
    torrents = {s: build(f"sp10-p{s}.mkv", s * MIB, s * MIB * PIECES[s]) for s in sizes}
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        children = []
        swarm: dict[str, list] = {"serving": [], "silent": [], "chatty": []}

        async def seed_service(key: str, host: str, profiles: list[dict]) -> None:
            spec = {"peers": len(profiles), "seed": 10, "host": host, "profiles": profiles, "torrents": torrents_spec}
            path = work / f"{key}-{host}.json"
            path.write_text(json.dumps(spec), encoding="utf-8")
            proc, endpoints = await start_child([str(HERE / "sp_seed_service.py"), "--spec", str(path),
                                                 "--stats", str(work / f"{key}-{host}.stats.json")])
            children.append((f"{key}-{host}", proc, work / f"{key}-{host}.stats.json"))
            swarm[key].extend(endpoints)

        await seed_service("serving", SERVING_HOST, SWARMS[args.swarm])
        kinds = {VARIANTS[n].get("stall") for n in names} - {None}
        if kinds:
            hosts = [{"host": h, "keepalive_s": None} for h in SILENT_HOSTS] if "silent" in kinds else []
            hosts += [{"host": h, "keepalive_s": CHATTY_KEEPALIVE_S} for h in CHATTY_HOSTS] if "chatty" in kinds else []
            stall_spec = work / "stall.json"
            stall_spec.write_text(json.dumps({"hosts": hosts, "torrents": torrents_spec,
                                              "stats": str(work / "stall.stats.json")}), encoding="utf-8")
            proc, endpoints = await start_child([str(Path(__file__).resolve()), "--stall-seed", str(stall_spec)])
            children.append(("stalled", proc, work / "stall.stats.json"))
            for item, endpoint in zip(hosts, endpoints, strict=True):
                swarm["silent" if item["keepalive_s"] is None else "chatty"].append(endpoint)

        runs = []
        for rep in range(args.reps):
            for size in sizes:
                for name in names:
                    t = time.monotonic()
                    result = await run_once(lt, args.scenario, name, size, torrents[size], swarm, args.cap_s)
                    result["rep"] = rep
                    runs.append(result)
                    print(f"[{time.monotonic() - started:7.1f}s] rep{rep} {size:2d}MiB {name:24s} "
                          f"pieces={result['piece_s']} stuck={result['stuck_blocks']} "
                          f"({time.monotonic() - t:.1f}s)", file=sys.stderr, flush=True)
                    await asyncio.sleep(0.3)
        seed_stats = {}
        for key, proc, stats_path in children:
            proc.terminate()
            await proc.wait()
            seed_stats[key] = json.loads(stats_path.read_text()) if stats_path.exists() else None
    print(json.dumps({
        "fixture": "sp-g0-sp10-stalled-peers-v1", "is_measurement": False, "native_version": lt.__version__,
        "config": {"scenario": args.scenario, "swarm": args.swarm, "serving_profiles": SWARMS[args.swarm],
                   "piece_mib": sizes, "pieces": {str(s): PIECES[s] for s in sizes},
                   "silent_hosts": SILENT_HOSTS, "chatty_hosts": CHATTY_HOSTS,
                   "chatty_keepalive_s": CHATTY_KEEPALIVE_S, "window3": WINDOW3, "cap_s": args.cap_s,
                   "reps": args.reps, "variants": {n: VARIANTS[n] for n in names}},
        "wall_s": round(time.monotonic() - started, 1), "runs": runs, "seed_stats": seed_stats,
    }, indent=1))


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--stall-seed":
        asyncio.run(stall_main(sys.argv[2]))
    else:
        asyncio.run(main(sys.argv[1:]))

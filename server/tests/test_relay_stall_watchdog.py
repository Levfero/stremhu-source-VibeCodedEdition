"""SP10: stalled-peer watchdog behind ``relay_stall_policy`` (default legacy = no native call).

G0 (g0-sp10-stalled-probe-v2.json, libtorrent 2.1.1): a peer that accepts block requests
but never answers holds deadline blocks until libtorrent snubs it, and a shorter
piece_timeout only moves the delay to the last piece. Banning a peer with outstanding
requests and no payload for 2 s (ip_filter) shortened the start without a false ban.
"""

import asyncio
import threading
from types import SimpleNamespace
from typing import Any, cast, get_args

import libtorrent
import pytest

from app.config import Config, config
from app.modules.relay import service as service_module
from app.modules.relay.service import RelayService
from app.modules.relay.stall_watchdog import PeerSample, StallWatchdog

H = "a" * 40
SERVING = "10.0.0.2"
STALLED = "10.0.0.9"


def peer(ip: str, total: int, queue: int = 4, port: int = 6881, info_hash: str = H) -> PeerSample:
    return PeerSample(info_hash, ip, port, queue, total)


def run_ticks(detector: StallWatchdog, until: float, stalled=(STALLED,), serving=(SERVING,),
              serving_progress: bool = True, step: float = 0.5) -> dict[float, list[str]]:
    """Ticks every ``step`` s from 0 to ``until``; serving peers gain 16 KiB per tick."""
    out: dict[float, list[str]] = {}
    tick = 0
    while tick * step <= until + 1e-9:
        now = tick * step
        samples = [peer(ip, 16384 * tick if serving_progress else 0) for ip in serving]
        samples += [peer(ip, 0) for ip in stalled if ip not in detector.bans]
        out[now] = detector.observe(samples, now)
        tick += 1
    return out


# --------------------------------------------------------------------------- detector


def test_progressing_peer_is_never_banned():
    detector = StallWatchdog(2.0, 120.0, 64)
    banned = run_ticks(detector, 10.0, stalled=())
    assert not any(banned.values()) and detector.bans == {}


def test_stalled_peer_is_banned_after_the_threshold_while_another_peer_progresses():
    detector = StallWatchdog(2.0, 120.0, 64)
    banned = run_ticks(detector, 2.0)
    assert banned[1.5] == []
    assert banned[2.0] == [STALLED]
    assert detector.bans == {STALLED: 122.0}


def test_peer_without_outstanding_requests_is_not_banned():
    detector = StallWatchdog(2.0, 120.0, 64)
    for tick in range(20):
        now = tick * 0.5
        assert detector.observe([peer(SERVING, 16384 * tick), peer(STALLED, 0, queue=0)], now) == []


def test_no_ban_when_no_peer_progresses():
    # Our own link stalls: every peer is silent, nobody may be banned.
    detector = StallWatchdog(2.0, 120.0, 64)
    banned = run_ticks(detector, 10.0, serving_progress=False)
    assert not any(banned.values()) and detector.bans == {}


def test_draining_the_queue_restarts_the_clock():
    detector = StallWatchdog(2.0, 120.0, 64)
    queue = {0.0: 4, 0.5: 4, 1.0: 4, 1.5: 0, 2.0: 4, 2.5: 4, 3.0: 4, 3.5: 4}
    banned = {}
    for tick, (now, length) in enumerate(queue.items()):
        banned[now] = detector.observe([peer(SERVING, 16384 * tick), peer(STALLED, 0, queue=length)], now)
    assert banned[2.0] == [] and banned[3.0] == []
    assert banned[3.5] == [STALLED]  # 2 s after the clock restarted at 1.5


def test_ban_expires_after_the_ban_time():
    detector = StallWatchdog(2.0, 120.0, 64)
    run_ticks(detector, 2.0)
    assert detector.expire(121.9) == []
    assert detector.expire(122.0) == [STALLED]
    assert detector.bans == {}


def test_ban_list_is_bounded():
    detector = StallWatchdog(2.0, 120.0, 2)
    banned = run_ticks(detector, 2.0, stalled=("10.0.0.7", "10.0.0.8", "10.0.0.9"))
    assert banned[2.0] == ["10.0.0.7", "10.0.0.8"]
    assert len(detector.bans) == 2 and detector.skipped_full == 1


def test_two_connections_from_one_address_are_one_ban():
    detector = StallWatchdog(2.0, 120.0, 64)
    banned = []
    for tick in range(5):
        now = tick * 0.5
        banned += detector.observe([peer(SERVING, 16384 * tick), peer(STALLED, 0, port=1),
                                    peer(STALLED, 0, port=2)], now)
    assert banned == [STALLED]


def test_reconnected_peer_starts_a_fresh_clock():
    detector = StallWatchdog(2.0, 120.0, 64)
    detector.observe([peer(SERVING, 0), peer(STALLED, 0, port=1)], 0.0)
    detector.observe([peer(SERVING, 16384)], 1.5)  # the stalled connection is gone: state pruned
    assert detector.observe([peer(SERVING, 32768), peer(STALLED, 0, port=2)], 2.0) == []
    assert detector.observe([peer(SERVING, 49152), peer(STALLED, 0, port=2)], 3.5) == []
    assert detector.observe([peer(SERVING, 65536), peer(STALLED, 0, port=2)], 4.0) == [STALLED]


def test_torrents_are_judged_separately():
    # Progress in another torrent does not count as "the swarm is alive" for this one.
    other = "b" * 40
    detector = StallWatchdog(2.0, 120.0, 64)
    for tick in range(6):
        now = tick * 0.5
        banned = detector.observe([peer(SERVING, 16384 * tick, info_hash=other), peer(STALLED, 0)], now)
        assert banned == []


# --------------------------------------------------------------------------- service


class Handle:
    def __init__(self):
        self.peers: list[SimpleNamespace] = []
        self.valid = True
        self.calls = 0

    def is_valid(self) -> bool:
        return self.valid

    def get_peer_info(self) -> list[SimpleNamespace]:
        self.calls += 1
        return list(self.peers)


class Session:
    def __init__(self):
        self.filters: list[Any] = []
        self.fail = False

    def set_ip_filter(self, ip_filter) -> None:
        if self.fail:
            self.fail = False
            raise RuntimeError("synthetic native failure")
        self.filters.append(ip_filter)


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.setattr(config, "relay_stall_policy", "watchdog")
    monkeypatch.setattr(config, "relay_stall_watchdog_seconds", 2.0)
    monkeypatch.setattr(config, "relay_stall_ban_seconds", 120.0)
    monkeypatch.setattr(config, "relay_stall_ban_max", 64)
    service = object.__new__(RelayService)
    service._connection_policy_lock = threading.RLock()
    service._stall_lock = threading.Lock()
    service._stall_watchdog = None
    service._stall_filter_synced = True
    session = Session()
    service._libtorrent_session = cast(Any, session)
    handle = Handle()
    torrent = SimpleNamespace(info_hash=H, has_active_streams=True, torrent_handle=handle)
    service._torrents = cast(Any, {libtorrent.sha1_hash(bytes.fromhex(H)): torrent})
    return service, session, handle, torrent


def set_peers(handle: Handle, tick: int, stalled: bool = True) -> None:
    handle.peers = [SimpleNamespace(ip=(SERVING, 6881), download_queue_length=8, total_download=16384 * tick)]
    if stalled:
        handle.peers.append(SimpleNamespace(ip=(STALLED, 6881), download_queue_length=4, total_download=0))


def blocked(ip_filter, ip: str) -> bool:
    return ip_filter.access(ip) == 1


def test_legacy_policy_issues_no_native_call(relay, monkeypatch):
    monkeypatch.setattr(config, "relay_stall_policy", "legacy")
    service, session, handle, _ = relay
    for tick in range(10):
        set_peers(handle, tick)
        service.stall_watchdog_tick(now=tick * 0.5)
    service.clear_stall_bans()
    assert handle.calls == 0 and session.filters == []


def test_stalled_peer_is_filtered_once_and_released_after_the_ban(relay):
    service, session, handle, _ = relay
    for tick in range(10):
        set_peers(handle, tick, stalled=tick <= 4)  # a banned peer is disconnected by libtorrent
        service.stall_watchdog_tick(now=tick * 0.5)
    assert len(session.filters) == 1  # rebuilt only on change
    assert blocked(session.filters[0], STALLED) and not blocked(session.filters[0], SERVING)
    service.stall_watchdog_tick(now=2.0 + 120.0)
    assert len(session.filters) == 2 and not blocked(session.filters[1], STALLED)


def test_without_stalled_peers_nothing_is_filtered(relay):
    service, session, handle, _ = relay
    for tick in range(20):
        set_peers(handle, tick, stalled=False)
        service.stall_watchdog_tick(now=tick * 0.5)
    assert session.filters == [] and handle.calls == 20


def test_torrent_without_active_stream_is_not_queried(relay):
    service, session, handle, torrent = relay
    torrent.has_active_streams = False
    for tick in range(10):
        set_peers(handle, tick)
        service.stall_watchdog_tick(now=tick * 0.5)
    assert handle.calls == 0 and session.filters == []


def test_invalid_handle_is_skipped(relay):
    service, session, handle, _ = relay
    handle.valid = False
    for tick in range(10):
        set_peers(handle, tick)
        service.stall_watchdog_tick(now=tick * 0.5)
    assert handle.calls == 0 and session.filters == []


def test_failed_filter_update_is_retried_on_the_next_tick(relay):
    service, session, handle, _ = relay
    for tick in range(4):
        set_peers(handle, tick)
        service.stall_watchdog_tick(now=tick * 0.5)
    set_peers(handle, 4)
    session.fail = True
    with pytest.raises(RuntimeError, match="synthetic native failure"):
        service.stall_watchdog_tick(now=2.0)
    assert session.filters == []
    set_peers(handle, 5, stalled=False)
    service.stall_watchdog_tick(now=2.5)
    assert len(session.filters) == 1 and blocked(session.filters[0], STALLED)


def test_cleanup_empties_the_filter_once(relay):
    service, session, handle, _ = relay
    for tick in range(5):
        set_peers(handle, tick)
        service.stall_watchdog_tick(now=tick * 0.5)
    service.clear_stall_bans()
    service.clear_stall_bans()
    assert len(session.filters) == 2 and not blocked(session.filters[1], STALLED)
    assert service._stall_watchdog is not None and service._stall_watchdog.bans == {}


def test_cleanup_without_bans_or_after_close_issues_no_native_call(relay):
    service, session, handle, _ = relay
    service.clear_stall_bans()  # never ticked
    set_peers(handle, 0)
    service.stall_watchdog_tick(now=0.0)
    service.clear_stall_bans()  # ticked, nothing banned (this also restarts the detector clock)
    for tick in range(1, 6):
        set_peers(handle, tick)
        service.stall_watchdog_tick(now=tick * 0.5)
    filters = len(session.filters)
    service._closed = True
    service.stall_watchdog_tick(now=200.0)
    service.clear_stall_bans()
    assert filters == 1 and len(session.filters) == 1


@pytest.mark.anyio
async def test_loop_survives_a_failing_tick_and_cleans_up_on_cancel(relay, monkeypatch, caplog):
    service, *_ = relay
    monkeypatch.setattr(service_module, "STALL_WATCHDOG_INTERVAL_SECONDS", 0)
    calls = {"tick": 0, "clear": 0}
    ticked = asyncio.Event()

    def tick() -> None:
        calls["tick"] += 1
        if calls["tick"] == 1:
            raise RuntimeError("synthetic native failure")
        ticked.set()

    def clear() -> None:
        calls["clear"] += 1

    service.stall_watchdog_tick = tick
    service.clear_stall_bans = clear
    task = asyncio.create_task(service.stall_watchdog_loop())
    await asyncio.wait_for(ticked.wait(), 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls["tick"] >= 2 and calls["clear"] == 1
    assert any("Stall watchdog failed" in record.getMessage() for record in caplog.records)


@pytest.mark.anyio
async def test_loop_stops_when_the_service_closes(relay, monkeypatch):
    service, *_ = relay
    monkeypatch.setattr(service_module, "STALL_WATCHDOG_INTERVAL_SECONDS", 0)
    cleared = []

    def tick() -> None:
        service._closed = True

    service.stall_watchdog_tick = tick
    service.clear_stall_bans = lambda: cleared.append(True)
    await asyncio.wait_for(service.stall_watchdog_loop(), 5)
    assert cleared == [True]


def test_defaults_keep_todays_behaviour():
    fields = Config.model_fields
    assert fields["relay_stall_policy"].default == "legacy"
    assert set(get_args(fields["relay_stall_policy"].annotation)) == {"legacy", "watchdog"}
    assert fields["relay_stall_watchdog_seconds"].default == 2.0
    assert fields["relay_stall_ban_seconds"].default == 120.0  # technical default, documented
    assert fields["relay_stall_ban_max"].default == 64

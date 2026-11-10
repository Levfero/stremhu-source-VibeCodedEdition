"""SP10: stream-scoped session piece_timeout behind ``relay_stall_policy`` (default legacy).

G0 (g0-sp10-stalled-probe.json, libtorrent 2.1.1): a peer that accepts block requests
but never answers is snubbed ``piece_timeout`` seconds after its first request, and only
then do its deadline blocks move to another peer. The candidate shortens the session
setting while any stream is active and restores today's value afterwards. Switched off,
nothing changes: no native session call, the same deadlines (today's edges/upstream start).
"""

import threading
from types import SimpleNamespace
from typing import Annotated, Any, cast

import libtorrent
import pytest
from pydantic import TypeAdapter, ValidationError

from app.config import Config, config
from app.modules.relay.entities import File, Torrent
from app.modules.relay.service import PIECE_TIMEOUT_SECONDS, RelayService
from tests.test_relay_start_and_readahead_policy import (
    MIB,
    FakeHandle,
    make_torrent,
    stream_at,
)

HASH_A = "a" * 40
HASH_B = "b" * 40


class Handle(FakeHandle):
    def __init__(self, pieces: int):
        super().__init__(pieces)
        self.valid = True
        self.limit: int | None = None

    def is_valid(self) -> bool:
        return self.valid

    def set_max_connections(self, value: int) -> None:
        self.limit = value


class Session:
    def __init__(self):
        self.applied: list[dict] = []
        self.fail = False
        self.handles: dict[str, Handle] = {}
        self.removed: list[Handle] = []

    def apply_settings(self, settings: dict) -> None:
        if self.fail:
            self.fail = False
            raise RuntimeError("synthetic native failure")
        self.applied.append(dict(settings))

    def find_torrent(self, info_hash):
        return self.handles.get(str(info_hash), SimpleNamespace(is_valid=lambda: False))

    def remove_torrent(self, handle, flags) -> None:
        self.removed.append(handle)


@pytest.fixture
def relay(monkeypatch):
    # Today's start (gsp02-v4 PASS:C2); the stall switch is independent of it.
    monkeypatch.setattr(config, "relay_start_policy", "edges")
    monkeypatch.setattr(config, "relay_readahead_policy", "upstream")
    monkeypatch.setattr(config, "relay_stall_policy", "stream_piece_timeout")
    monkeypatch.setattr(config, "relay_stream_piece_timeout_seconds", 3)
    service = object.__new__(RelayService)
    service._connection_policy_lock = threading.RLock()
    service._torrent_connections_limit = 20
    service._applied_piece_timeout = PIECE_TIMEOUT_SECONDS
    session = Session()
    service._libtorrent_session = cast(Any, session)
    service._torrents = {}
    service._loop = cast(Any, SimpleNamespace(call_soon_threadsafe=lambda *args: None))

    def add(info_hash: str, pieces: int = 20) -> tuple[Torrent, File, Handle]:
        torrent, file, _ = make_torrent(MIB, pieces)
        handle = Handle(pieces)
        torrent.torrent_handle = cast(Any, handle)
        torrent.info_hash = info_hash
        torrent.service = service
        torrent._last_applied_limit = None
        torrent._last_applied_handle = None
        service._torrents[libtorrent.sha1_hash(bytes.fromhex(info_hash))] = torrent
        session.handles[info_hash] = handle
        return torrent, file, handle

    return service, session, add


def piece_timeouts(session: Session) -> list[int]:
    return [settings["piece_timeout"] for settings in session.applied]


def test_legacy_policy_never_changes_the_session_setting(relay, monkeypatch):
    monkeypatch.setattr(config, "relay_stall_policy", "legacy")
    service, session, add = relay
    torrent, file, handle = add(HASH_A)
    stream = stream_at(torrent, file, 5)
    torrent.priority_manager()
    # today's edges/upstream deadlines (test_relay_start_and_readahead_policy)
    assert handle.deadlines == {0: 0, 19: 0, 5: 0, 6: 3000, 7: 4000, 8: 5000}
    del file.streams[stream.id]
    torrent.priority_manager()
    service.delete_torrent(HASH_A)
    assert session.applied == []
    assert service._applied_piece_timeout == PIECE_TIMEOUT_SECONDS


@pytest.mark.parametrize("piece", [0, 5, 19])
def test_switch_does_not_change_the_deadlines(relay, monkeypatch, piece):
    _, _, add = relay
    results = []
    for policy in ("legacy", "stream_piece_timeout"):
        monkeypatch.setattr(config, "relay_stall_policy", policy)
        torrent, file, handle = add(HASH_A)
        stream_at(torrent, file, piece)
        torrent.priority_manager()
        results.append((dict(handle.deadlines), list(handle.priority)))
    assert results[0] == results[1]


def test_default_switch_is_todays_behaviour():
    fields = Config.model_fields
    assert fields["relay_stall_policy"].default == "legacy"
    assert fields["relay_stream_piece_timeout_seconds"].default == 3


@pytest.mark.parametrize(("value", "valid"), [(0, False), (1, True), (10, True), (11, False)])
def test_stream_piece_timeout_is_bounded(value, valid):
    # Only a shorter-or-equal timeout than today's 10 s is a candidate; 0 would disable snubbing.
    field = Config.model_fields["relay_stream_piece_timeout_seconds"]
    adapter = TypeAdapter(Annotated[(int, *field.metadata)])
    if valid:
        assert adapter.validate_python(value) == value
    else:
        with pytest.raises(ValidationError):
            adapter.validate_python(value)


def test_active_stream_shortens_the_timeout_once_and_restores_it_after_the_last_stream(relay):
    _, session, add = relay
    torrent, file, handle = add(HASH_A)
    stream = stream_at(torrent, file, 0)
    torrent.priority_manager()
    torrent.priority_manager()  # e.g. the next finished piece: no repeated native call
    assert piece_timeouts(session) == [3]
    assert handle.deadlines[0] == 0  # the deadlines are set as before
    del file.streams[stream.id]
    torrent.priority_manager()
    torrent.priority_manager()
    assert piece_timeouts(session) == [3, PIECE_TIMEOUT_SECONDS]


def test_timeout_stays_short_while_any_torrent_streams(relay):
    _, session, add = relay
    first, first_file, _ = add(HASH_A)
    second, second_file, _ = add(HASH_B)
    first_stream = stream_at(first, first_file, 0)
    second_stream = stream_at(second, second_file, 0)
    first.priority_manager()
    second.priority_manager()
    del first_file.streams[first_stream.id]
    first.priority_manager()
    assert piece_timeouts(session) == [3]
    del second_file.streams[second_stream.id]
    second.priority_manager()
    assert piece_timeouts(session) == [3, PIECE_TIMEOUT_SECONDS]


def test_removing_a_streaming_torrent_restores_the_timeout(relay):
    service, session, add = relay
    torrent, file, handle = add(HASH_A)
    stream_at(torrent, file, 0)
    torrent.priority_manager()
    assert service.delete_torrent(HASH_A)
    assert session.removed == [handle]
    assert piece_timeouts(session) == [3, PIECE_TIMEOUT_SECONDS]


def test_failed_native_call_keeps_the_deadlines_and_is_retried(relay):
    service, session, add = relay
    torrent, file, handle = add(HASH_A)
    stream_at(torrent, file, 0)
    session.fail = True
    torrent.priority_manager()
    assert handle.deadlines[0] == 0  # deadlines were applied before the failing session call
    assert session.applied == [] and service._applied_piece_timeout is None
    torrent.priority_manager()
    assert piece_timeouts(session) == [3]


def test_configured_value_is_applied(relay, monkeypatch):
    monkeypatch.setattr(config, "relay_stream_piece_timeout_seconds", 2)
    _, session, add = relay
    torrent, file, _ = add(HASH_A)
    stream_at(torrent, file, 0)
    torrent.priority_manager()
    assert piece_timeouts(session) == [2]


def test_destroying_stream_still_counts_until_it_is_removed(relay):
    # Stream.destroy: is_destroying first, removal 0.25 s later; both trigger the priority manager.
    _, session, add = relay
    torrent, file, _ = add(HASH_A)
    stream = stream_at(torrent, file, 0)
    torrent.priority_manager()
    stream.is_destroying = True
    torrent.priority_manager()
    assert piece_timeouts(session) == [3]
    del file.streams[stream.id]
    torrent.priority_manager()
    assert piece_timeouts(session) == [3, PIECE_TIMEOUT_SECONDS]


def test_each_change_is_logged_once_for_the_gate_wiring_check(relay, caplog):
    _, _, add = relay
    torrent, file, _ = add(HASH_A)
    stream = stream_at(torrent, file, 0)
    with caplog.at_level("INFO"):
        torrent.priority_manager()
        torrent.priority_manager()
        del file.streams[stream.id]
        torrent.priority_manager()
    lines = [r.getMessage() for r in caplog.records if "Relay stall policy" in r.getMessage()]
    assert lines == ["Relay stall policy: piece_timeout=3 s (active streams: True)",
                     f"Relay stall policy: piece_timeout={PIECE_TIMEOUT_SECONDS} s (active streams: False)"]


def test_closed_service_issues_no_native_call(relay):
    service, session, add = relay
    torrent, file, _ = add(HASH_A)
    stream_at(torrent, file, 0)
    service._closed = True
    service.apply_stall_policy()
    assert session.applied == []

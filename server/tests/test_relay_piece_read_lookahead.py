"""SP04: overlapping the next piece read with sending, behind relay_piece_read_lookahead_bytes.

The relay double is the real ``RelayService.get_piece_data`` path (shared task behind
``shield``, ``_pending_piece_reads``, retry/timeout, ``cancel_piece_requests``). Only the
native handle is a double: ``read_piece`` records the call and the test decides when and
how the ``read_piece_alert`` answers. The request double answers ``is_disconnected()``
from a script and yields to the loop like the real one.

0 (the default) must keep today's strictly serial order: piece N+1 is read only after
every chunk of piece N was sent.
"""

import asyncio
import gc
import logging
import random
from collections.abc import AsyncGenerator
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.common.async_utils import drain_background_tasks
from app.config import config
from app.modules.relay import entities
from app.modules.relay.entities import File, Stream, Torrent
from app.modules.relay.service import PieceReadAbortedError, RelayService

INFO_HASH = "c" * 40
PIECE = 64
CHUNK = 16
PIECES = 6
CONTENT = random.Random(4).randbytes(PIECE * PIECES)
# Range with a partial first (from byte 10) and last piece (up to byte 20 of piece 4).
START, END = 10, 4 * PIECE + 20

Event = tuple[str, int | bool | None]


@pytest.fixture(autouse=True)
def small_chunks(monkeypatch):
    monkeypatch.setattr(entities, "CHUNK_SIZE", CHUNK)


def set_lookahead(monkeypatch, value: int) -> None:
    monkeypatch.setattr(config, "relay_piece_read_lookahead_bytes", value)


class NativeHandle:
    """torrent_handle double; reads are answered through the read_piece_alert path."""

    def __init__(self, service: RelayService, events: list[Event]):
        self.service = service
        self.events = events
        self.have = set(range(PIECES))
        self.held: set[int] = set()
        self.failures: dict[int, Exception] = {}

    def info_hash(self) -> str:
        return INFO_HASH

    def have_piece(self, index: int) -> bool:
        return index in self.have

    def read_piece(self, index: int) -> None:
        self.events.append(("read", index))
        if index not in self.held:
            asyncio.get_running_loop().call_soon(self.answer, index)

    def answer(self, index: int) -> None:
        # The process_alerts read_piece_alert branch.
        future = self.service._pending_piece_reads.pop((INFO_HASH, index), None)
        if future is None or future.done():
            return
        if index in self.failures:
            future.set_exception(self.failures[index])
        else:
            future.set_result(CONTENT[index * PIECE : (index + 1) * PIECE])

    def release(self, index: int) -> None:
        self.held.discard(index)
        self.answer(index)

    def finish_download(self, index: int) -> None:
        # The process_alerts piece_finished_alert branch.
        self.have.add(index)
        if (INFO_HASH, index) in self.service._pending_piece_reads:
            self.read_piece(index)


class ScriptedRequest:
    def __init__(self, events: list[Event], disconnect_after_checks: int | None = None):
        self.events = events
        self.disconnect_after_checks = disconnect_after_checks
        self.checks = 0

    async def is_disconnected(self) -> bool:
        await asyncio.sleep(0)
        self.checks += 1
        limit = self.disconnect_after_checks
        disconnected = limit is not None and self.checks > limit
        self.events.append(("check", disconnected))
        return disconnected


class Rig:
    def __init__(self, monkeypatch, disconnect_after_checks: int | None = None):
        self.events: list[Event] = []
        service = object.__new__(RelayService)
        service._loop = asyncio.get_running_loop()
        service._piece_fetch_tasks = {}
        service._pending_piece_reads = {}
        monkeypatch.setattr(service, "trigger_priority_update", lambda _: self.events.append(("priority", None)))
        self.service = service
        self.handle = NativeHandle(service, self.events)
        torrent = object.__new__(Torrent)
        torrent.torrent_handle = cast(Any, self.handle)
        torrent.service = service
        torrent.info_hash = INFO_HASH
        torrent.piece_size = PIECE
        self.file = File(cast(Any, SimpleNamespace(name="movie.mkv", size=PIECE * PIECES, offset=0, index=0)),
                         torrent)
        torrent.files = {0: self.file}
        self.stream = Stream("s", "p", "u", torrent, self.file, START, END)
        self.request = ScriptedRequest(self.events, disconnect_after_checks)
        self.received = bytearray()

    async def iterator(self) -> AsyncGenerator[bytes, None]:
        # The _stream_inner generator itself: aclose() is what closing the response does to it.
        return cast(AsyncGenerator[bytes, None], await self.stream.start(cast(Any, self.request), raise_errors=True))

    async def consume(self, iterator, until=None) -> None:
        async for chunk in iterator:
            self.received += chunk
            self.events.append(("yield", self.stream.current_stream_piece))
            if until is not None and until(self):
                return

    def reads(self) -> list[int]:
        return [cast(int, value) for name, value in self.events if name == "read"]

    def index(self, event: Event, last: bool = False) -> int:
        positions = [i for i, item in enumerate(self.events) if item == event]
        return positions[-1] if last else positions[0]

    async def settle(self) -> None:
        for _ in range(20):
            await asyncio.sleep(0)
        await drain_background_tasks()


def serial_trace(disconnect_at: int | None = None) -> list[Event]:
    """Today's order, from the behaviour itself: per piece one priority update, a disconnect
    check and the read; then per chunk a disconnect check and the chunk. Nothing overlaps."""
    first, last = START // PIECE, END // PIECE
    trace: list[Event] = []
    for piece in range(first, last + 1):
        low = START % PIECE if piece == first else 0
        high = END % PIECE + 1 if piece == last else PIECE
        trace += [("priority", None), ("check", False), ("read", piece)]
        for _ in range(0, high - low, CHUNK):
            trace += [("check", False), ("yield", piece)]
    return trace


def record_loop_errors(errors: list[dict]) -> None:
    asyncio.get_running_loop().set_exception_handler(lambda _, context: errors.append(context))


@pytest.mark.parametrize("limit", [0, PIECE - 1], ids=["off", "piece-larger-than-limit"])
def test_off_keeps_todays_serial_reads(monkeypatch, limit):
    set_lookahead(monkeypatch, limit)

    async def scenario():
        rig = Rig(monkeypatch)
        await rig.consume(await rig.iterator())
        assert rig.events == serial_trace()
        assert bytes(rig.received) == CONTENT[START : END + 1]
        await rig.settle()
        assert rig.service._piece_fetch_tasks == {}
        assert rig.file.streams == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [PIECE, 16 * PIECE])
def test_on_reads_the_next_piece_while_sending_and_keeps_the_byte_order(monkeypatch, limit):
    set_lookahead(monkeypatch, limit)

    async def scenario():
        rig = Rig(monkeypatch)
        await rig.consume(await rig.iterator())
        first, last = START // PIECE, END // PIECE
        assert bytes(rig.received) == CONTENT[START : END + 1]
        # Every piece of the range exactly once, in order, never past the range.
        assert rig.reads() == list(range(first, last + 1))
        for piece in range(first, last):
            # Overlap: the next read is issued before the current piece's last chunk is sent ...
            assert rig.index(("read", piece + 1)) < rig.index(("yield", piece), last=True)
            # ... but never before the current piece's data arrived (one piece ahead at most).
            assert rig.index(("read", piece + 1)) > rig.index(("read", piece))
        highest = -1
        for name, value in rig.events:
            if name == "read":
                highest = max(highest, cast(int, value))
            elif name == "yield":
                assert highest <= cast(int, value) + 1
        await rig.settle()
        assert rig.service._piece_fetch_tasks == {}
        assert rig.service._pending_piece_reads == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("seed", range(8))
def test_on_keeps_the_byte_order_whatever_order_reads_complete_in(monkeypatch, seed):
    set_lookahead(monkeypatch, PIECE)

    async def scenario():
        rig = Rig(monkeypatch)
        rig.handle.held = set(range(PIECES))
        rng = random.Random(seed)
        done = asyncio.Event()

        async def disk() -> None:
            # Answers the issued reads in a random order after random delays.
            while not done.is_set():
                for _ in range(rng.randrange(1, 6)):
                    await asyncio.sleep(0)
                waiting = [key[1] for key in rig.service._pending_piece_reads]
                if waiting:
                    rig.handle.release(rng.choice(waiting))

        driver = asyncio.create_task(disk())
        await rig.consume(await rig.iterator())
        done.set()
        await driver
        assert bytes(rig.received) == CONTENT[START : END + 1]
        await rig.settle()
        assert rig.service._piece_fetch_tasks == {}

    asyncio.run(scenario())


def test_on_does_not_read_ahead_a_piece_that_is_still_downloading(monkeypatch):
    set_lookahead(monkeypatch, PIECE)

    async def scenario():
        rig = Rig(monkeypatch)
        rig.handle.have.discard(3)
        iterator = await rig.iterator()
        consumer = asyncio.create_task(rig.consume(iterator))
        # The stream reaches piece 3 and waits for its download (no read while piece 2 was sent).
        while (INFO_HASH, 3) not in rig.service._pending_piece_reads:
            await asyncio.sleep(0)
        assert 3 not in rig.reads()
        assert ("yield", 2) in rig.events and rig.stream.current_stream_piece == 3
        rig.handle.finish_download(3)
        await consumer
        assert bytes(rig.received) == CONTENT[START : END + 1]
        assert rig.index(("read", 3)) > rig.index(("yield", 2), last=True)
        await rig.settle()

    asyncio.run(scenario())


def checks_in_full_run() -> int:
    return sum(1 for name, _ in serial_trace() if name == "check")


@pytest.mark.parametrize("limit", [0, PIECE], ids=["off", "on"])
@pytest.mark.parametrize("checks_before_disconnect", range(checks_in_full_run()))
def test_no_new_read_starts_after_the_disconnect_is_seen(monkeypatch, limit, checks_before_disconnect):
    set_lookahead(monkeypatch, limit)

    async def scenario():
        rig = Rig(monkeypatch, disconnect_after_checks=checks_before_disconnect)
        await rig.consume(await rig.iterator())
        seen = rig.index(("check", True))
        sent = [cast(int, value) for name, value in rig.events[:seen] if name == "yield"]
        await rig.settle()
        after = rig.events[seen:]
        assert all(name != "yield" for name, _ in after)
        # A shared read is issued asynchronously, so a read started before the disconnect was
        # seen may land after it. Off: none is in flight then. On: only the single lookahead
        # started while the last sent piece went out; nothing further is ever read.
        in_flight = {sent[-1] + 1} if limit and sent else set()
        assert {value for name, value in after if name == "read"} <= in_flight
        # Never more than one piece beyond the last piece sent (off: the piece due; on: the lookahead).
        bound = sent[-1] + 1 if sent else START // PIECE
        assert all(piece <= bound for piece in rig.reads())
        assert rig.service._piece_fetch_tasks == {}
        assert rig.service._pending_piece_reads == {}
        assert rig.file.streams == {}

    asyncio.run(scenario())


def test_a_failed_lookahead_surfaces_only_when_its_piece_is_due(monkeypatch):
    set_lookahead(monkeypatch, PIECE)

    async def scenario():
        rig = Rig(monkeypatch)
        rig.handle.failures[2] = Exception("synthetic native read error")
        iterator = await rig.iterator()
        await rig.consume(iterator, until=lambda r: ("read", 2) in r.events)
        assert rig.events[-1] == ("yield", 1)
        # The read of piece 2 fails completely while piece 1 is still being sent ...
        while (INFO_HASH, 2) in rig.service._piece_fetch_tasks:
            await asyncio.sleep(0)
        for _ in range(5):
            await asyncio.sleep(0)
        # ... and piece 1 still goes out in full before the error appears.
        with pytest.raises(Exception, match="synthetic native read error"):
            await rig.consume(iterator)
        assert bytes(rig.received) == CONTENT[START : 2 * PIECE]
        await rig.settle()
        assert rig.service._piece_fetch_tasks == {}

    asyncio.run(scenario())


def test_a_failed_lookahead_is_dropped_silently_when_the_stream_ends_before_it(monkeypatch):
    set_lookahead(monkeypatch, PIECE)
    errors: list[dict] = []

    async def scenario():
        record_loop_errors(errors)
        rig = Rig(monkeypatch)
        rig.handle.failures[2] = Exception("synthetic native read error")
        iterator = await rig.iterator()
        # The player closes the connection during piece 1, after the read of piece 2 failed.
        await rig.consume(iterator, until=lambda r: ("read", 2) in r.events and r.events[-1] == ("yield", 1))
        while (INFO_HASH, 2) in rig.service._piece_fetch_tasks:
            await asyncio.sleep(0)
        for _ in range(5):
            await asyncio.sleep(0)  # the stream's own lookahead has finished with the error, unread
        await iterator.aclose()
        await rig.settle()
        assert bytes(rig.received) == CONTENT[START : START + len(rig.received)]
        assert rig.service._piece_fetch_tasks == {}

    asyncio.run(scenario())
    gc.collect()
    assert errors == []


def test_torrent_removal_still_raises_piece_read_aborted_for_a_pending_lookahead(monkeypatch):
    set_lookahead(monkeypatch, PIECE)

    async def scenario():
        rig = Rig(monkeypatch)
        rig.handle.held.add(2)
        iterator = await rig.iterator()
        await rig.consume(iterator, until=lambda r: ("read", 2) in r.events)
        assert rig.stream.current_stream_piece == 1
        rig.service.cancel_piece_requests(INFO_HASH)
        with pytest.raises(PieceReadAbortedError):
            await rig.consume(iterator)
        assert bytes(rig.received) == CONTENT[START : 2 * PIECE]
        await rig.settle()
        assert rig.service._piece_fetch_tasks == {}
        assert rig.service._pending_piece_reads == {}

    asyncio.run(scenario())


@pytest.mark.parametrize("limit", [0, PIECE], ids=["off", "on"])
def test_torrent_removal_still_raises_piece_read_aborted_for_the_awaited_piece(monkeypatch, limit):
    set_lookahead(monkeypatch, limit)

    async def scenario():
        rig = Rig(monkeypatch)
        rig.handle.held.add(0)
        iterator = await rig.iterator()
        consumer = asyncio.create_task(rig.consume(iterator))
        while (INFO_HASH, 0) not in rig.service._pending_piece_reads:
            await asyncio.sleep(0)
        rig.service.cancel_piece_requests(INFO_HASH)
        with pytest.raises(PieceReadAbortedError):
            await consumer
        assert rig.received == b""
        await rig.settle()

    asyncio.run(scenario())


def test_no_task_outlives_the_stream_and_the_shared_read_runs_to_its_end(monkeypatch, caplog):
    set_lookahead(monkeypatch, PIECE)
    errors: list[dict] = []

    async def scenario():
        record_loop_errors(errors)
        baseline = asyncio.all_tasks()
        rig = Rig(monkeypatch)
        rig.handle.held.add(3)
        iterator = await rig.iterator()
        # Disconnect while piece 2 is sent and the read of piece 3 is still pending.
        await rig.consume(iterator, until=lambda r: ("read", 3) in r.events)
        await iterator.aclose()
        for _ in range(5):
            await asyncio.sleep(0)
        shared = rig.service._piece_fetch_tasks[(INFO_HASH, 3)]
        assert not shared.done()  # the stream dropped only its own reference
        await drain_background_tasks()
        assert asyncio.all_tasks() - baseline == {shared}
        rig.handle.release(3)
        assert await shared == CONTENT[3 * PIECE : 4 * PIECE]
        await asyncio.sleep(0)
        assert rig.service._piece_fetch_tasks == {}
        assert rig.service._pending_piece_reads == {}
        assert asyncio.all_tasks() - baseline == set()
        assert rig.file.streams == {}

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    gc.collect()
    assert errors == []
    assert "Background work failed" not in caplog.text


def test_no_task_outlives_a_stream_that_ran_to_the_end(monkeypatch):
    set_lookahead(monkeypatch, PIECE)

    async def scenario():
        baseline = asyncio.all_tasks()
        rig = Rig(monkeypatch)
        await rig.consume(await rig.iterator())
        await rig.settle()
        assert asyncio.all_tasks() - baseline == set()
        assert rig.service._piece_fetch_tasks == {}

    asyncio.run(scenario())


def test_default_is_off():
    assert type(config).model_fields["relay_piece_read_lookahead_bytes"].default == 0

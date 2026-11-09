"""SP03: playback history is written once per playback, off the response path.

Repeated Range/HEAD requests of one playback do not touch the database; a failed
write is retried by the next request; a full pending set falls back to a
synchronous write; shutdown drains pending writes. The DB write is a thread-safe
double; scheduling, locking and the real background-task drain are the product's.
"""

import asyncio
import logging
import threading
from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.common.async_utils import drain_background_tasks
from app.modules.playback_histories.schemas.internal import PlaybackHistoryClientInfo
from app.modules.stream import service as stream_service
from app.modules.stream.schemas import StreamToken
from app.modules.stream.service import StreamService, clear_playback_history_state


class _Writes:
    def __init__(self, fail_first: int = 0, gate: threading.Event | None = None):
        self.calls: list[str] = []
        self.fail_first = fail_first
        self.gate = gate
        self.lock = threading.Lock()

    def __call__(self, payload) -> None:
        if self.gate is not None:
            assert self.gate.wait(5)
        with self.lock:
            self.calls.append(payload.playback_id)
            if len(self.calls) <= self.fail_first:
                raise RuntimeError("synthetic write failure")


@pytest.fixture
def history(monkeypatch):
    clear_playback_history_state()

    def install(writes: _Writes) -> _Writes:
        monkeypatch.setattr(stream_service, "_persist_playback_history", writes)
        return writes

    yield install
    clear_playback_history_state()


def _save(playback_id: str):
    service = object.__new__(StreamService)
    token = StreamToken(indexer_id="fixture", torrent_id="1", file_index=0, playback_id=playback_id)
    file = cast(Any, SimpleNamespace(name="movie.mkv", torrent=SimpleNamespace(name="movie")))
    return service.save_playback_history(token, PlaybackHistoryClientInfo(user_agent=None, ip=None), "user", file)


@pytest.mark.anyio
async def test_response_path_does_not_wait_for_the_write(history):
    gate = threading.Event()
    writes = history(_Writes(gate=gate))
    await asyncio.wait_for(_save("p1"), 1)  # returns although the write is blocked
    assert writes.calls == []
    gate.set()
    await drain_background_tasks()
    assert writes.calls == ["p1"]


@pytest.mark.anyio
async def test_repeated_requests_write_once(history):
    writes = history(_Writes())
    for _ in range(10):
        await _save("p2")
    await drain_background_tasks()
    for _ in range(10):
        await _save("p2")
    await drain_background_tasks()
    assert writes.calls == ["p2"]


@pytest.mark.anyio
async def test_failed_write_is_retried_by_the_next_request(history, caplog):
    caplog.set_level(logging.WARNING, logger="app.common.logger")
    writes = history(_Writes(fail_first=1))
    await _save("p3")
    await drain_background_tasks()
    await _save("p3")
    await drain_background_tasks()
    await _save("p3")
    await drain_background_tasks()
    assert writes.calls == ["p3", "p3"]
    assert "playback-history-write" in caplog.text
    assert "synthetic write failure" not in caplog.text


@pytest.mark.anyio
async def test_full_pending_set_falls_back_to_a_synchronous_write(history, monkeypatch):
    writes = history(_Writes())
    monkeypatch.setattr(stream_service, "_HISTORY_PENDING_MAX", 0)
    await _save("p4")
    assert writes.calls == ["p4"]  # written before the call returned


@pytest.mark.anyio
async def test_distinct_playbacks_are_each_written(history):
    writes = history(_Writes())
    await asyncio.gather(*(_save(f"p5-{i}") for i in range(5)))
    await drain_background_tasks()
    assert sorted(writes.calls) == [f"p5-{i}" for i in range(5)]

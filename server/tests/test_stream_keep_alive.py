"""SP09: `Connection` header of stream responses and connection reuse under Hypercorn."""

import asyncio
import socket
import time
from contextlib import contextmanager
from unittest.mock import AsyncMock, Mock

import h11
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypercorn.asyncio import serve
from hypercorn.config import Config as HypercornConfig

from app.config import config
from app.main import app
from app.modules.stream import active_playback, router
from app.modules.stream.schemas import StreamToken
from app.modules.stream.service import parse_range_header
from app.modules.stream.utils.stream_token import generate_stream_token
from app.modules.users.models import UserModel

FILE_BYTES = bytes(range(256)) * 4  # 1024 bytes


@contextmanager
def fixture_db():
    yield None


def install_stream_fixture(monkeypatch, *, short_by: int = 0) -> str:
    """Stream endpoint with stub services; the file body is FILE_BYTES, `short_by` bytes cut from each response."""
    active_playback.clear_active_playbacks()
    user = UserModel(id="keep-alive-user", username="fixture")
    auth = Mock()
    auth.verify_api_key.return_value = user
    file = Mock(size=len(FILE_BYTES))
    file.name = "fixture.mkv"

    async def file_stream(*, stream_start_byte, stream_end_byte, **kwargs):
        async def body():
            # As the relay after a piece failure: the stream ends early and in order.
            yield FILE_BYTES[stream_start_byte : stream_end_byte + 1 - short_by]

        return body()

    file.stream = AsyncMock(side_effect=file_stream)
    stream = Mock()

    async def prepare_for_stream(*, range_header, **kwargs):
        return parse_range_header(len(FILE_BYTES), range_header), file

    stream.prepare_for_stream = AsyncMock(side_effect=prepare_for_stream)
    stream.save_playback_history = AsyncMock()
    monkeypatch.setattr(router, "isolated_db_session", fixture_db)
    monkeypatch.setattr(router, "create_auth_service", lambda db: auth)
    monkeypatch.setattr(router, "get_relay_service", lambda: None)
    monkeypatch.setattr(router, "create_playback_histories_service", lambda db: None)
    monkeypatch.setattr(router, "create_playbacks_service", lambda **kwargs: Mock())
    monkeypatch.setattr(router, "create_stream_service", lambda db: stream)
    token = generate_stream_token(StreamToken(indexer_id="keep-alive", torrent_id="1", file_index=0, playback_id="fixture"))
    return f"/api/fixture/stream/{token}"


@pytest.mark.parametrize("keep_alive", [False, True])
@pytest.mark.parametrize(
    ("method", "range_value", "status"),
    [("GET", None, 200), ("GET", "bytes=0-9", 206), ("HEAD", "bytes=0-9", 206), ("HEAD", None, 200)],
)
def test_connection_header_by_switch_and_status(monkeypatch, keep_alive, method, range_value, status):
    monkeypatch.setattr(config, "stream_keep_alive", keep_alive)
    path = install_stream_fixture(monkeypatch)
    headers = {"Range": range_value} if range_value else {}
    response = TestClient(app).request(method, path, headers=headers)
    assert response.status_code == status
    expected = None if keep_alive and status == 206 else "close"
    assert response.headers.get("connection") == expected
    assert response.headers["accept-ranges"] == "bytes"
    if method == "GET":
        assert response.content == (FILE_BYTES if range_value is None else FILE_BYTES[:10])
    active_playback.clear_active_playbacks()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def start_server(asgi_app: FastAPI) -> tuple[int, asyncio.Event, asyncio.Task]:
    port = free_port()
    hypercorn_config = HypercornConfig()
    hypercorn_config.bind = [f"127.0.0.1:{port}"]
    hypercorn_config.accesslog = None
    shutdown = asyncio.Event()
    task = asyncio.create_task(serve(asgi_app, hypercorn_config, shutdown_trigger=shutdown.wait))  # pyright: ignore[reportArgumentType]
    deadline = time.monotonic() + 10
    while True:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.close()
            return port, shutdown, task
        except OSError:
            if time.monotonic() > deadline:
                raise
            await asyncio.sleep(0.05)


async def exchange(conn: h11.Connection, reader, writer, path: str, range_value: str) -> tuple[int, bytes, bool]:
    """One request on an open connection; returns (status, body, server closed after the response)."""
    request = h11.Request(method="GET", target=path, headers=[("Host", "fixture"), ("Range", range_value)])
    writer.write(conn.send(request) + conn.send(h11.EndOfMessage()))
    await writer.drain()
    status, body = 0, b""
    while True:
        event = conn.next_event()
        if event is h11.NEED_DATA:
            conn.receive_data(await asyncio.wait_for(reader.read(65536), 3))
            continue
        if isinstance(event, h11.Response):
            status = event.status_code
        elif isinstance(event, h11.Data):
            body += bytes(event.data)
        elif isinstance(event, h11.EndOfMessage):
            break
    closed = conn.our_state is h11.MUST_CLOSE or conn.their_state is h11.MUST_CLOSE
    return status, body, closed


async def eof_within(reader, seconds: float) -> bool:
    try:
        return await asyncio.wait_for(reader.read(65536), seconds) == b""
    except TimeoutError:
        return False


def stream_only_app() -> FastAPI:
    asgi_app = FastAPI()
    asgi_app.include_router(router.router, prefix="/api")
    return asgi_app


@pytest.mark.anyio
async def test_keep_alive_reuses_one_connection_for_bounded_ranges(monkeypatch):
    monkeypatch.setattr(config, "stream_keep_alive", True)
    path = install_stream_fixture(monkeypatch)
    port, shutdown, task = await start_server(stream_only_app())
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        conn = h11.Connection(our_role=h11.CLIENT)
        for start in (0, 1000, 500):
            status, body, closed = await exchange(conn, reader, writer, path, f"bytes={start}-{start + 9}")
            assert (status, body, closed) == (206, FILE_BYTES[start : start + 10], False)
            conn.start_next_cycle()
        writer.close()
    finally:
        shutdown.set()
        await task
        active_playback.clear_active_playbacks()


@pytest.mark.anyio
async def test_default_closes_after_each_response(monkeypatch):
    monkeypatch.setattr(config, "stream_keep_alive", False)
    path = install_stream_fixture(monkeypatch)
    port, shutdown, task = await start_server(stream_only_app())
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        conn = h11.Connection(our_role=h11.CLIENT)
        status, body, closed = await exchange(conn, reader, writer, path, "bytes=0-9")
        assert (status, body, closed) == (206, FILE_BYTES[:10], True)
        assert await eof_within(reader, 2)
        writer.close()
    finally:
        shutdown.set()
        await task
        active_playback.clear_active_playbacks()


@pytest.mark.anyio
async def test_keep_alive_closes_at_once_after_a_short_body(monkeypatch):
    """Headers promise 10 bytes, the relay delivers 5: the server must close, not idle until keep-alive timeout."""
    monkeypatch.setattr(config, "stream_keep_alive", True)
    path = install_stream_fixture(monkeypatch, short_by=5)
    port, shutdown, task = await start_server(stream_only_app())
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        conn = h11.Connection(our_role=h11.CLIENT)
        request = h11.Request(method="GET", target=path, headers=[("Host", "fixture"), ("Range", "bytes=0-9")])
        writer.write(conn.send(request) + conn.send(h11.EndOfMessage()))
        await writer.drain()
        received = b""
        started = time.monotonic()
        while (chunk := await asyncio.wait_for(reader.read(65536), 3)) != b"":
            received += chunk
        assert time.monotonic() - started < 2  # Hypercorn's keep-alive timeout is 5 s
        head, _, body = received.partition(b"\r\n\r\n")
        assert head.startswith(b"HTTP/1.1 206")
        assert b"content-length: 10" in head.lower()
        assert body == FILE_BYTES[:5]
        writer.close()
    finally:
        shutdown.set()
        await task
        active_playback.clear_active_playbacks()

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import HTTPException

from app.modules.relay.network_policy import VpnProxyPolicy
from app.modules.settings.schemas.internal import VpnSettings
from app.modules.vpn.manager import VpnManager, VpnState, VpnStatus
from app.modules.vpn.secret_store import SecretStore
from app.modules.vpn.service import VpnService
from app.modules.vpn.speedtest import (
    SocksAuth,
    SpeedTestError,
    SpeedTestResult,
    VpnSpeedTest,
)

pytestmark = pytest.mark.anyio

USER, PASSWORD = "fixture-user", "fixture-pass"


async def _read_request(reader: asyncio.StreamReader) -> str:
    head = await reader.readuntil(b"\r\n\r\n")
    return head.split(b" ", 2)[1].decode()


class FakeTunnel:
    """SOCKS5-proxy, amely a kapcsolat után maga válaszol HTTP-n (mintha a tunnel túloldala lenne)."""

    def __init__(self, *, exit_ip="203.0.113.7", body_bytes=2_000_000, stall=False):
        self.exit_ip = exit_ip
        self.body_bytes = body_bytes
        self.stall = stall
        self.targets: list[tuple[str, int]] = []
        self.server: asyncio.Server | None = None
        self.closing = asyncio.Event()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc):
        assert self.server is not None
        self.closing.set()
        self.server.close()
        await self.server.wait_closed()

    @property
    def port(self) -> int:
        assert self.server is not None
        return self.server.sockets[0].getsockname()[1]

    async def _handle(self, reader, writer):
        try:
            await reader.readexactly(3)
            writer.write(b"\x05\x02")
            await writer.drain()
            await reader.readexactly(1)
            user = await reader.readexactly((await reader.readexactly(1))[0])
            password = await reader.readexactly((await reader.readexactly(1))[0])
            if (user.decode(), password.decode()) != (USER, PASSWORD):
                writer.write(b"\x01\x01")
                await writer.drain()
                return
            writer.write(b"\x01\x00")
            await reader.readexactly(4)
            name = await reader.readexactly((await reader.readexactly(1))[0])
            port = int.from_bytes(await reader.readexactly(2), "big")
            self.targets.append((name.decode(), port))
            writer.write(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
            await writer.drain()
            path = await _read_request(reader)
            if path.startswith("/cdn-cgi/trace"):
                body = f"fl=1\nip={self.exit_ip}\nloc=HU\n".encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
            else:
                if self.stall:
                    await self.closing.wait()
                    return
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % self.body_bytes)
                chunk = b"x" * 65536
                sent = 0
                while sent < self.body_bytes:
                    part = chunk[: self.body_bytes - sent]
                    writer.write(part)
                    sent += len(part)
                    await writer.drain()
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def _direct_trace_server(ip: str):
    async def handle(reader, writer):
        try:
            await _read_request(reader)
            body = f"ip={ip}\nloc=HU\n".encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(body), body))
            await writer.drain()
        finally:
            writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


def _speed_test(direct_port: int, **kwargs) -> VpnSpeedTest:
    return VpnSpeedTest(
        download_url=f"http://localhost:{direct_port}/__down?bytes=25000000",
        trace_url=f"http://localhost:{direct_port}/cdn-cgi/trace",
        **kwargs,
    )


async def test_measures_through_the_proxy_with_hostname_resolution_in_the_tunnel():
    direct = await _direct_trace_server("198.51.100.9")
    direct_port = direct.sockets[0].getsockname()[1]
    try:
        async with FakeTunnel() as tunnel:
            result = await _speed_test(direct_port).run(SocksAuth(tunnel.port, USER, PASSWORD))
    finally:
        direct.close()
    assert result.downloaded_bytes == 2_000_000
    assert result.download_mbps > 0 and result.duration_seconds >= 0
    assert result.latency_ms is not None
    assert (result.exit_ip, result.exit_country) == ("203.0.113.7", "HU")
    assert result.exit_differs_from_direct is True
    # A név a proxyhoz megy (a tunnel DNS-e oldja fel), nem helyben feloldott cím.
    assert tunnel.targets == [("localhost", direct_port), ("localhost", direct_port)]


async def test_same_exit_and_direct_address_is_reported_and_missing_direct_is_unknown():
    direct = await _direct_trace_server("203.0.113.7")
    direct_port = direct.sockets[0].getsockname()[1]
    try:
        async with FakeTunnel() as tunnel:
            same = await _speed_test(direct_port).run(SocksAuth(tunnel.port, USER, PASSWORD))
    finally:
        direct.close()
        await direct.wait_closed()
    assert same.exit_differs_from_direct is False

    async with FakeTunnel() as tunnel:
        unknown = await _speed_test(direct_port).run(SocksAuth(tunnel.port, USER, PASSWORD))
    assert unknown.exit_differs_from_direct is None


async def test_download_stops_at_the_byte_limit():
    async with FakeTunnel(body_bytes=3_000_000) as tunnel:
        result = await _speed_test(9, max_bytes=1_000_000).run(SocksAuth(tunnel.port, USER, PASSWORD))
    assert result.downloaded_bytes == 1_000_000


async def test_wrong_proxy_credentials_and_stalled_download_fail_with_safe_codes():
    async with FakeTunnel() as tunnel:
        with pytest.raises(SpeedTestError) as refused:
            await _speed_test(9).run(SocksAuth(tunnel.port, USER, "wrong"))
    assert refused.value.code == "proxy-refused"

    async with FakeTunnel(stall=True) as tunnel:
        with pytest.raises(SpeedTestError) as stalled:
            await _speed_test(9, timeout=0.5).run(SocksAuth(tunnel.port, USER, PASSWORD))
    assert stalled.value.code == "timeout"


def test_only_http_urls_are_accepted():
    with pytest.raises(ValueError):
        VpnSpeedTest(download_url="file:///etc/passwd", trace_url="https://example.invalid/trace")


class _Manager:
    def __init__(self, state=VpnState.CONNECTED):
        self.status = VpnStatus(state=state, active_config_id="a")
        self.policy = VpnProxyPolicy(port=41234, username=USER, password=PASSWORD)


class _SpeedTest:
    def __init__(self, outcome, gate: asyncio.Event | None = None):
        self.outcome = outcome
        self.gate = gate
        self.auth: SocksAuth | None = None

    async def run(self, auth: SocksAuth):
        self.auth = auth
        if self.gate is not None:
            await self.gate.wait()
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def _service(manager, speed_test) -> VpnService:
    return VpnService(
        cast(VpnManager, manager),
        load_settings=VpnSettings,
        save_settings=lambda update: VpnSettings(),
        secret_store_factory=lambda: cast(SecretStore, SimpleNamespace()),
        network_settings=lambda: {},
        speed_test_factory=lambda: cast(VpnSpeedTest, speed_test),
        speed_test_lock=asyncio.Lock(),
    )


RESULT = SpeedTestResult(
    download_mbps=66.0,
    downloaded_bytes=25_000_000,
    duration_seconds=3.0,
    latency_ms=35.0,
    exit_ip="203.0.113.7",
    exit_country="HU",
    exit_differs_from_direct=True,
)


async def test_service_runs_only_on_a_connected_vpn_one_at_a_time():
    with pytest.raises(HTTPException) as idle:
        await _service(_Manager(VpnState.RECONNECTING), _SpeedTest(RESULT)).speed_test()
    assert idle.value.status_code == 409

    fake = _SpeedTest(RESULT)
    response = await _service(_Manager(), fake).speed_test()
    assert response.config_id == "a" and response.download_mbps == 66.0
    assert fake.auth == SocksAuth(port=41234, username=USER, password=PASSWORD)

    gate = asyncio.Event()
    service = _service(_Manager(), _SpeedTest(RESULT, gate))
    first = asyncio.create_task(service.speed_test())
    await asyncio.sleep(0.01)
    with pytest.raises(HTTPException) as busy:
        await service.speed_test()
    assert busy.value.status_code == 409
    gate.set()
    await first


async def test_service_turns_failures_into_502_with_the_code():
    with pytest.raises(HTTPException) as failed:
        await _service(_Manager(), _SpeedTest(SpeedTestError("timeout"))).speed_test()
    assert failed.value.status_code == 502
    assert "timeout" in str(failed.value.detail)

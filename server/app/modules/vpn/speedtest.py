"""VPN-sebességteszt (F6): letöltés a helyi wireproxy SOCKS5-proxyján, azaz a tunnelen át.

A hostnevet a proxy (a tunnel DNS-e) oldja fel, a TLS a proxykapcsolaton belül épül fel.
A mérés felhasználói kérésre fut, méret- és időkorláttal. A kilépő IP-t egy `trace`
végpont adja; a saját (közvetlen) IP-címet csak összehasonlításra kérdezzük le, soha
nem adjuk vissza és nem naplózzuk.
"""

import asyncio
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

USER_AGENT = "StremHU-VPN-speedtest"
MAX_HEADER_BYTES = 16 * 1024
MAX_TRACE_BYTES = 4 * 1024


class SpeedTestError(Exception):
    """Titokmentes hibakód a felület számára."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SocksAuth:
    port: int
    username: str
    password: str
    host: str = "127.0.0.1"


@dataclass(frozen=True)
class SpeedTestResult:
    download_mbps: float
    downloaded_bytes: int
    duration_seconds: float
    latency_ms: float | None
    exit_ip: str | None
    exit_country: str | None
    exit_differs_from_direct: bool | None


@dataclass(frozen=True)
class _Target:
    host: str
    port: int
    path: str
    tls: bool


def _target(url: str) -> _Target:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("A sebességteszt URL-je csak http(s) lehet.")
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return _Target(
        host=parts.hostname,
        port=parts.port or (443 if parts.scheme == "https" else 80),
        path=path,
        tls=parts.scheme == "https",
    )


async def _socks_connect(auth: SocksAuth, host: str, port: int):
    """SOCKS5 CONNECT felhasználónév/jelszó hitelesítéssel (RFC 1928, 1929), tartománynévvel."""
    reader, writer = await asyncio.open_connection(auth.host, auth.port)
    try:
        writer.write(b"\x05\x01\x02")
        await writer.drain()
        if await reader.readexactly(2) != b"\x05\x02":
            raise SpeedTestError("proxy-refused")
        user, password = auth.username.encode(), auth.password.encode()
        writer.write(b"\x01" + bytes([len(user)]) + user + bytes([len(password)]) + password)
        await writer.drain()
        if (await reader.readexactly(2))[1] != 0:
            raise SpeedTestError("proxy-refused")
        name = host.encode("idna")
        writer.write(b"\x05\x01\x00\x03" + bytes([len(name)]) + name + port.to_bytes(2, "big"))
        await writer.drain()
        head = await reader.readexactly(4)
        if head[1] != 0:
            raise SpeedTestError("proxy-connect-failed")
        address_length = {1: 4, 4: 16}.get(head[3])
        if address_length is None:
            address_length = (await reader.readexactly(1))[0]
        await reader.readexactly(address_length + 2)
    except (asyncio.IncompleteReadError, ConnectionError, OSError) as error:
        writer.close()
        raise SpeedTestError("proxy-refused") from error
    except BaseException:
        writer.close()
        raise
    return reader, writer


async def _open(target: _Target, socks: SocksAuth | None, ssl_context: ssl.SSLContext):
    if socks is not None:
        reader, writer = await _socks_connect(socks, target.host, target.port)
    else:
        # Közvetlen összehasonlító kérés: IPv4, hogy a két cím összevethető legyen.
        reader, writer = await asyncio.open_connection(target.host, target.port, family=socket.AF_INET)
    if target.tls:
        try:
            await writer.start_tls(ssl_context, server_hostname=target.host)
        except (ssl.SSLError, OSError) as error:
            writer.close()
            raise SpeedTestError("tls-error") from error
    return reader, writer


async def _request(reader, writer, target: _Target) -> tuple[float, bytes]:
    """GET kérés; visszaadja az első válaszbájtig eltelt időt (s) és a fejléc utáni első adatot."""
    request = (
        f"GET {target.path} HTTP/1.1\r\nHost: {target.host}\r\nUser-Agent: {USER_AGENT}\r\n"
        "Accept-Encoding: identity\r\nConnection: close\r\n\r\n"
    )
    started = time.perf_counter()
    writer.write(request.encode("ascii"))
    await writer.drain()
    buffer = await reader.read(64 * 1024)
    first_byte = time.perf_counter() - started
    while b"\r\n\r\n" not in buffer:
        if not buffer or len(buffer) > MAX_HEADER_BYTES:
            raise SpeedTestError("http-error")
        chunk = await reader.read(64 * 1024)
        if not chunk:
            raise SpeedTestError("http-error")
        buffer += chunk
    head, _, rest = buffer.partition(b"\r\n\r\n")
    status = head.split(b"\r\n", 1)[0].split()
    if len(status) < 2 or status[1] != b"200":
        raise SpeedTestError("http-error")
    return first_byte, rest


async def _trace(target: _Target, socks: SocksAuth | None, ssl_context) -> tuple[float, dict[str, str]]:
    reader, writer = await _open(target, socks, ssl_context)
    try:
        first_byte, body = await _request(reader, writer, target)
        while len(body) < MAX_TRACE_BYTES:
            chunk = await reader.read(MAX_TRACE_BYTES)
            if not chunk:
                break
            body += chunk
    finally:
        writer.close()
    fields = {}
    for line in body[:MAX_TRACE_BYTES].decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            fields[key.strip()] = value.strip()
    return first_byte, fields


def _ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(value) if value else None
    except ValueError:
        return None


class VpnSpeedTest:
    def __init__(
        self,
        *,
        download_url: str,
        trace_url: str,
        max_bytes: int = 25_000_000,
        max_seconds: float = 15.0,
        timeout: float = 30.0,
        ssl_context: ssl.SSLContext | None = None,
    ):
        self._download = _target(download_url)
        self._trace = _target(trace_url)
        self._max_bytes = max_bytes
        self._max_seconds = max_seconds
        self._timeout = timeout
        self._ssl_context = ssl_context or ssl.create_default_context()

    async def run(self, socks: SocksAuth) -> SpeedTestResult:
        try:
            async with asyncio.timeout(self._timeout):
                return await self._run(socks)
        except TimeoutError:
            raise SpeedTestError("timeout") from None
        except (ConnectionError, OSError) as error:
            raise SpeedTestError("network-error") from error

    async def _run(self, socks: SocksAuth) -> SpeedTestResult:
        latency, fields = await _trace(self._trace, socks, self._ssl_context)
        exit_ip = _ip(fields.get("ip"))
        downloaded, duration = await self._measure(socks)
        direct_ip = await self._direct_ip()
        differs = None
        if exit_ip is not None and direct_ip is not None and exit_ip.version == direct_ip.version:
            differs = exit_ip != direct_ip
        return SpeedTestResult(
            download_mbps=round(downloaded * 8 / duration / 1_000_000, 1) if duration > 0 else 0.0,
            downloaded_bytes=downloaded,
            duration_seconds=round(duration, 2),
            latency_ms=round(latency * 1000, 1),
            exit_ip=str(exit_ip) if exit_ip else None,
            exit_country=(fields.get("loc") or None),
            exit_differs_from_direct=differs,
        )

    async def _measure(self, socks: SocksAuth) -> tuple[int, float]:
        reader, writer = await _open(self._download, socks, self._ssl_context)
        try:
            _, first = await _request(reader, writer, self._download)
            started = time.perf_counter()
            downloaded = len(first)
            while downloaded < self._max_bytes and time.perf_counter() - started < self._max_seconds:
                chunk = await reader.read(256 * 1024)
                if not chunk:
                    break
                downloaded += len(chunk)
            duration = time.perf_counter() - started
        finally:
            writer.close()
        if downloaded == 0:
            raise SpeedTestError("http-error")
        return min(downloaded, self._max_bytes), duration

    async def _direct_ip(self):
        """A saját IP a normál hálózaton, csak az összehasonlításhoz; hibánál None."""
        try:
            _, fields = await _trace(self._trace, None, self._ssl_context)
        except (SpeedTestError, ConnectionError, OSError, asyncio.IncompleteReadError):
            return None
        return _ip(fields.get("ip"))

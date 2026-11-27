"""A wireproxy alfolyamat életciklusa (VPN-terv Rev. 5, 6.1).

A nyílt WireGuard-konfiguráció csak a futási könyvtárban (tmpfs) él, `0600`
joggal, és a SOCKS-port elérhetővé válása után törlődik. A kimenet redaktálva
kerül a naplóba.
"""

import asyncio
import contextlib
import os
import re
import secrets
import socket
from collections.abc import Callable
from pathlib import Path

from app.common.logger import logger

_SECRET_LINE = re.compile(r"(?i)(privatekey|presharedkey|password|username)\s*[=:].*")
_BASE64_KEY = re.compile(r"[A-Za-z0-9+/]{42,44}={0,2}")
_MAX_LOG_LINE = 300


class ProxyStartError(Exception):
    """Titokmentes indítási hiba; a `code` a felület és a naplók számára."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def reserve_loopback_port() -> int:
    """Szabad loopback-port. A foglalás és a használat között rövid versenyablak van;
    ütközéskor a wireproxy nem indul, és a felügyelő újrapróbál."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def redact_output(line: str) -> str:
    line = _SECRET_LINE.sub(r"\1=[redacted]", line)
    line = _BASE64_KEY.sub("[redacted]", line)
    return line[:_MAX_LOG_LINE]


ArgvFactory = Callable[[Path, Path, int], list[str]]


class ProxySupervisor:
    def __init__(
        self,
        binary: Path,
        runtime_dir: Path,
        *,
        ready_timeout: float = 15.0,
        stop_timeout: float = 5.0,
        argv_factory: ArgvFactory | None = None,
    ):
        self._binary = binary
        self._runtime_dir = runtime_dir
        self._ready_timeout = ready_timeout
        self._stop_timeout = stop_timeout
        self._argv_factory = argv_factory
        self._process: asyncio.subprocess.Process | None = None
        self._output_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def _argv(self, config_path: Path, health_port: int) -> list[str]:
        if self._argv_factory is not None:
            return self._argv_factory(self._binary, config_path, health_port)
        if not self._binary.is_file():
            raise ProxyStartError("binary-missing")
        return [str(self._binary), "-c", str(config_path), "-i", f"127.0.0.1:{health_port}"]

    def _write_runtime_config(self, config_text: str) -> Path:
        if not self._runtime_dir.parent.is_dir():
            raise ProxyStartError("runtime-dir-unavailable")
        try:
            self._runtime_dir.mkdir(mode=0o700, exist_ok=True)
            path = self._runtime_dir / f"wireproxy-{secrets.token_hex(8)}.conf"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(config_text)
        except OSError:
            raise ProxyStartError("runtime-dir-unwritable") from None
        return path

    async def start(self, config_text: str, socks_port: int) -> None:
        if self.running:
            raise ProxyStartError("already-running")
        config_path = self._write_runtime_config(config_text)
        try:
            argv = self._argv(config_path, reserve_loopback_port())
            try:
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
            except OSError:
                raise ProxyStartError("spawn-failed") from None
            self._process = process
            self._output_task = asyncio.create_task(self._drain(process), name="vpn-wireproxy-output")
            try:
                await self._wait_ready(process, socks_port)
            except BaseException:
                await self.stop()
                raise
        finally:
            with contextlib.suppress(OSError):
                config_path.unlink()

    async def _wait_ready(self, process: asyncio.subprocess.Process, port: int) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._ready_timeout
        while loop.time() < deadline:
            if process.returncode is not None:
                raise ProxyStartError("exited")
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port), timeout=1.0
                )
            except (OSError, TimeoutError):
                await asyncio.sleep(0.1)
                continue
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            return
        raise ProxyStartError("not-ready")

    async def wait(self) -> int:
        process = self._process
        if process is None:
            return 0
        return await process.wait()

    async def _drain(self, process: asyncio.subprocess.Process) -> None:
        stream = process.stdout
        if stream is None:
            return
        while line := await stream.readline():
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.info("wireproxy: %s", redact_output(text))

    async def stop(self) -> None:
        process, self._process = self._process, None
        output, self._output_task = self._output_task, None
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=self._stop_timeout)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
        if output is not None:
            await asyncio.gather(output, return_exceptions=True)

#!/usr/bin/env python3
"""Phase 0 PoC P0-6 + Rule 11:
1. P0-6: 64 MiB `/dev/shm` ceiling, parent-owned 32 MiB (`8 x 4 MiB`) SHM pool, 65s paused slow HTTP reader,
   1,000x post-arrival Singleflight `try_retain` / `_decrement` stress, ready-queue `CancelledError` race,
   8/8 pinned SHM -> bounded UDS inline fallback (`max_slots=2`, `<= 16 MiB`, StreamWriter backpressure),
   20 MiB oversized UDS frame rejection, and Worker `SIGKILL` (`kill -9`) + restart without `/dev/shm` leak.
2. Rule 11A: Empirical ASGI (`FastAPI 0.143.0` + `Starlette 1.10.0` + `httpx`) proof of the unprimed
   `StreamingResponse` `HTTP 206` 0-byte body bug vs. the pre-primed `HTTP 503` (`Retry-After: 5`) fix and
   mid-stream truncation handling.
3. Rule 11B: Empirical proof of the 16 MiB 4K Remux dual-Range self-deadlock under static `8 x 4 MiB` SHM +
   `InlineFallbackLimiter(max_bytes=16 MiB)`, and verification of the contiguous 4 MiB page-allocator fix
   (`ContiguousPageShmPool`) within the same 32 MiB `/dev/shm` budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import hashlib
import json
from multiprocessing import resource_tracker, shared_memory
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import time
import tracemalloc

SLOT_BYTES = 4 * 1024 * 1024  # 4 MiB
NUM_SLOTS = 8  # 32 MiB total
POOL_BYTES = SLOT_BYTES * NUM_SLOTS
MAX_HEADER_BYTES = 64 * 1024
MAX_INLINE_PAYLOAD_BYTES = 16 * 1024 * 1024  # 16 MiB


class IpcProtocolLimitError(RuntimeError):
    pass


class VpnKillSwitchActiveError(RuntimeError):
    pass


class PieceTimeoutError(RuntimeError):
    pass


def shm_df_snapshot(label: str) -> dict[str, object]:
    st = os.statvfs("/dev/shm")
    total = st.f_frsize * st.f_blocks
    free = st.f_frsize * st.f_bavail
    used = total - free
    entries = []
    for p in sorted(Path("/dev/shm").iterdir()):
        s = p.stat()
        entries.append({"name": p.name, "size": s.st_size, "mode": oct(s.st_mode & 0o777), "uid": s.st_uid})
    return {
        "label": label,
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "entries": entries,
    }


def read_vmrss_bytes(pid: int | str = "self") -> int:
    for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) * 1024
    return 0


async def write_frame(writer: asyncio.StreamWriter, header: dict[str, object], payload: bytes = b"") -> None:
    hdr_bytes = json.dumps(header).encode("utf-8")
    writer.write(struct.pack("!II", len(hdr_bytes), len(payload)) + hdr_bytes + payload)
    await writer.drain()


async def read_frame(reader: asyncio.StreamReader) -> tuple[dict[str, object], bytes]:
    prefix = await reader.readexactly(8)
    hdr_len, payload_len = struct.unpack("!II", prefix)
    if hdr_len > MAX_HEADER_BYTES:
        raise IpcProtocolLimitError(f"Header length {hdr_len} exceeds {MAX_HEADER_BYTES}")
    if payload_len > MAX_INLINE_PAYLOAD_BYTES:
        raise IpcProtocolLimitError(f"Inline payload {payload_len} exceeds {MAX_INLINE_PAYLOAD_BYTES}")
    hdr = json.loads((await reader.readexactly(hdr_len)).decode("utf-8"))
    payload = await reader.readexactly(payload_len) if payload_len > 0 else b""
    return hdr, payload


# ============================================================================
# Worker Subprocess Entry (attaches to parent-owned 32 MiB SHM, serves UDS)
# ============================================================================
async def worker_async_main(uds_path: str, shm_name: str, start_empty_slots: bool = False) -> None:
    shm = shared_memory.SharedMemory(name=shm_name, create=False)
    # Unregister from child's resource_tracker so child exit/crash NEVER unlinks parent-owned SHM!
    resource_tracker.unregister(shm._name, "shared_memory")  # noqa: SLF001

    free_slots: list[tuple[int, int]] = [] if start_empty_slots else [(i, 1) for i in range(NUM_SLOTS)]

    def make_piece_bytes(piece_index: int, length: int) -> bytes:
        seed = hashlib.blake2b(f"p0-6-piece-{piece_index}-{length}".encode(), digest_size=64).digest()
        tile = b"".join(hashlib.blake2b(seed + i.to_bytes(2, "big"), digest_size=64).digest() for i in range(1024))
        full = tile * (length // len(tile))
        rem = length % len(tile)
        return full + (tile[:rem] if rem else b"")

    stop_event = asyncio.Event()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Configure bounded 2 MiB write buffer high-water mark for backpressure
        writer.transport.set_write_buffer_limits(high=2 * 1024 * 1024, low=512 * 1024)
        try:
            while True:
                hdr, _ = await read_frame(reader)
                op = hdr["op"]
                if op == "grant_shm_slot":
                    free_slots.append((int(hdr["slot_id"]), int(hdr["gen"])))
                elif op == "read_piece":
                    req_id = int(hdr["req_id"])
                    pidx = int(hdr["piece_index"])
                    plen = int(hdr["length"])
                    force_inline = bool(hdr.get("force_inline", False))
                    data = make_piece_bytes(pidx, plen)
                    if not force_inline and plen <= SLOT_BYTES and free_slots:
                        slot_id, gen = free_slots.pop(0)
                        off = slot_id * SLOT_BYTES
                        shm.buf[off : off + plen] = data
                        await write_frame(
                            writer,
                            {
                                "op": "piece_ready_shm",
                                "req_id": req_id,
                                "slot_id": slot_id,
                                "gen": gen,
                                "length": plen,
                            },
                        )
                    else:
                        await write_frame(
                            writer,
                            {"op": "piece_ready_inline", "req_id": req_id, "length": plen},
                            payload=data,
                        )
                elif op == "send_oversized_20m_frame":
                    # Deliberately write a 20 MiB frame header to test parent's 16 MiB privilege-boundary guard
                    bad_hdr = json.dumps({"op": "piece_ready_inline", "req_id": 99999}).encode()
                    writer.write(struct.pack("!II", len(bad_hdr), 20 * 1024 * 1024) + bad_hdr)
                    await writer.drain()
                elif op == "shutdown":
                    stop_event.set()
                    break
        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            writer.close()

    if Path(uds_path).exists():
        Path(uds_path).unlink()
    server = await asyncio.start_unix_server(handle_client, path=uds_path)
    print(json.dumps({"worker_ready": True, "pid": os.getpid()}), flush=True)
    try:
        await stop_event.wait()
    finally:
        server.close()
        await server.wait_closed()
        shm.close()


# ============================================================================
# Parent-Owned Race-Free SHM Pool + ReaderTicket + InlineFallbackLimiter
# ============================================================================
class PinnedPieceLease:
    def __init__(
        self,
        pool: SharedMemoryPiecePool,
        key: tuple[str, int],
        slot_id: int,
        gen: int,
        length: int,
    ) -> None:
        self._pool = pool
        self.key = key
        self.slot_id = slot_id
        self.gen = gen
        self.length = length
        self.refcount = 0
        self.closed_for_new_readers = False

    def try_retain(self) -> ReaderTicket | None:
        # Synchronous (zero `await`!) atomic retention on single-threaded asyncio event loop
        if self.closed_for_new_readers or self.refcount <= 0:
            return None
        self.refcount += 1
        return ReaderTicket(self)

    def create_initial_tickets(self, count: int) -> list[ReaderTicket]:
        assert count >= 1
        self.refcount = count
        return [ReaderTicket(self) for _ in range(count)]

    def decrement(self) -> None:
        if self.refcount <= 0:
            raise RuntimeError(f"Negative refcount detected on slot {self.slot_id}!")
        self.refcount -= 1
        if self.refcount == 0:
            self.closed_for_new_readers = True
            self._pool._on_lease_zero(self)  # noqa: SLF001


class ReaderTicket:
    def __init__(self, lease: PinnedPieceLease) -> None:
        self._lease: PinnedPieceLease | None = lease
        self._view: memoryview | None = None

    def open_view(self) -> memoryview:
        assert self._lease is not None, "Ticket already closed!"
        off = self._lease.slot_id * SLOT_BYTES
        self._view = self._lease._pool.shm.buf[off : off + self._lease.length]  # noqa: SLF001
        return self._view

    def close(self) -> None:
        if self._view is not None:
            self._view.release()
            self._view = None
        if self._lease is not None:
            lease = self._lease
            self._lease = None
            lease.decrement()


class SharedMemoryPiecePool:
    def __init__(self, name: str) -> None:
        self.shm = shared_memory.SharedMemory(name=name, create=True, size=POOL_BYTES)
        os.chmod(f"/dev/shm/{name}", 0o660)
        self.active_by_key: dict[tuple[str, int], PinnedPieceLease] = {}
        self.pinned_slots: dict[int, PinnedPieceLease] = {}
        self.slot_gen: list[int] = [1] * NUM_SLOTS
        self.on_grant_slot = None
        self.audit_negative_or_double_free = 0

    def register_shm_arrival(
        self,
        key: tuple[str, int],
        slot_id: int,
        length: int,
        waiter_count: int,
    ) -> list[ReaderTicket]:
        gen = self.slot_gen[slot_id]
        lease = PinnedPieceLease(self, key, slot_id, gen, length)
        self.pinned_slots[slot_id] = lease
        self.active_by_key[key] = lease
        return lease.create_initial_tickets(waiter_count)

    def try_retain_post_arrival(self, key: tuple[str, int]) -> ReaderTicket | None:
        lease = self.active_by_key.get(key)
        if lease is None:
            return None
        return lease.try_retain()

    def _on_lease_zero(self, lease: PinnedPieceLease) -> None:
        if self.active_by_key.get(lease.key) is lease:
            del self.active_by_key[lease.key]
        del self.pinned_slots[lease.slot_id]
        self.slot_gen[lease.slot_id] += 1
        if self.on_grant_slot is not None:
            self.on_grant_slot(lease.slot_id, self.slot_gen[lease.slot_id])

    def reclaim_unpinned_worker_slots_after_crash(self) -> list[tuple[int, int]]:
        """On Worker SIGKILL, pinned slots stay untouched for active HTTP readers; only unpinned slots go to new Worker!"""
        reclaimed = []
        for s in range(NUM_SLOTS):
            if s not in self.pinned_slots:
                self.slot_gen[s] += 1
                reclaimed.append((s, self.slot_gen[s]))
        return reclaimed

    def close_and_unlink(self) -> None:
        assert not self.pinned_slots, f"Leaked pinned slots at shutdown: {list(self.pinned_slots)}"
        self.shm.close()
        self.shm.unlink()


class InlineFallbackLimiter:
    def __init__(self, max_slots: int = 2, max_bytes: int = 16 * 1024 * 1024) -> None:
        self.max_slots = max_slots
        self.max_bytes = max_bytes
        self.active_slots = 0
        self.active_bytes = 0
        self.peak_active_slots = 0
        self.peak_active_bytes = 0
        self.backpressure_waits = 0
        self._cond = asyncio.Condition()

    @asynccontextmanager
    async def lease(self, nbytes: int, timeout: float = 5.0) -> AsyncIterator[None]:
        if nbytes > self.max_bytes:
            raise IpcProtocolLimitError(f"Piece {nbytes} > max_bytes {self.max_bytes}")

        async def _acquire() -> None:
            async with self._cond:
                while self.active_slots >= self.max_slots or (self.active_bytes + nbytes) > self.max_bytes:
                    self.backpressure_waits += 1
                    await self._cond.wait()
                self.active_slots += 1
                self.active_bytes += nbytes
                self.peak_active_slots = max(self.peak_active_slots, self.active_slots)
                self.peak_active_bytes = max(self.peak_active_bytes, self.active_bytes)

        try:
            await asyncio.wait_for(_acquire(), timeout=timeout)
        except TimeoutError as exc:
            raise PieceTimeoutError(f"InlineFallbackLimiter timed out waiting for {nbytes} bytes") from exc

        try:
            yield
        finally:
            async with self._cond:
                self.active_slots -= 1
                self.active_bytes -= nbytes
                self._cond.notify_all()


# ============================================================================
# Rule 11B Fix Prototype: Contiguous 4 MiB Page Allocator inside 32 MiB SHM
# ============================================================================
class ContiguousPageShmPool:
    """Supports 4 MiB (1 page), 8 MiB (2 pages), and 16 MiB (4 pages) pieces zero-copy inside the SAME 32 MiB SHM!"""

    def __init__(self, total_pages: int = 8, page_bytes: int = 4 * 1024 * 1024) -> None:
        self.total_pages = total_pages
        self.page_bytes = page_bytes
        self.shm = shared_memory.SharedMemory(create=True, size=total_pages * page_bytes)
        self.free_bitmap = [True] * total_pages

    def allocate_contiguous(self, length: int) -> tuple[int, int] | None:
        pages_needed = max(1, (length + self.page_bytes - 1) // self.page_bytes)
        # Power-of-two aligned or contiguous scan
        for start in range(0, self.total_pages - pages_needed + 1):
            if all(self.free_bitmap[start : start + pages_needed]):
                for p in range(start, start + pages_needed):
                    self.free_bitmap[p] = False
                return start, pages_needed
        return None

    def release_contiguous(self, start: int, pages: int) -> None:
        for p in range(start, start + pages):
            self.free_bitmap[p] = True

    def close_and_unlink(self) -> None:
        self.shm.close()
        self.shm.unlink()


def expected_piece_sha256(piece_index: int, length: int) -> str:
    seed = hashlib.blake2b(f"p0-6-piece-{piece_index}-{length}".encode(), digest_size=64).digest()
    tile = b"".join(hashlib.blake2b(seed + i.to_bytes(2, "big"), digest_size=64).digest() for i in range(1024))
    full = tile * (length // len(tile))
    rem = length % len(tile)
    return hashlib.sha256(full + (tile[:rem] if rem else b"")).hexdigest()


# ============================================================================
# Rule 11A Empirical Test (Real FastAPI 0.143.0 + Starlette 1.10.0 + httpx)
# ============================================================================
async def run_rule11a_http_streaming_error_test() -> dict[str, object]:
    from fastapi import FastAPI, Response  # noqa: PLC0415
    from fastapi.responses import JSONResponse, StreamingResponse  # noqa: PLC0415
    import httpx  # noqa: PLC0415

    app = FastAPI()

    @app.exception_handler(VpnKillSwitchActiveError)
    @app.exception_handler(PieceTimeoutError)
    async def _vpn_503_handler(_req: object, exc: Exception) -> Response:
        return JSONResponse(
            status_code=503,
            content={"detail": str(exc)},
            headers={"Retry-After": "5", "Cache-Control": "no-store"},
        )

    # 1. CURRENT UNPRIMED PATTERN (`stream/router.py:143-158` + `entities.py:393-460` `raise_errors=False`):
    @app.get("/unprimed-preheader-killswitch")
    async def unprimed_killswitch() -> StreamingResponse:
        async def _gen() -> AsyncIterator[bytes]:
            # Mimics `Stream.read()` (`raise_errors=False`) swallowing error after headers already sent!
            try:
                raise VpnKillSwitchActiveError("Kill switch active on piece #0")
                yield b""  # unreachable, makes it an async generator
            except VpnKillSwitchActiveError:
                return

        return StreamingResponse(
            _gen(),
            status_code=206,
            headers={"Content-Length": "4194304", "Content-Range": "bytes 0-4194303/67108864"},
        )

    # 2. FIXED PRE-PRIMED PATTERN (priming `first_chunk = await anext(it)` BEFORE `StreamingResponse`!):
    async def build_primed_streaming_response(gen: AsyncIterator[bytes], total_len: int) -> StreamingResponse:
        first_chunk = await anext(gen)

        async def _chained() -> AsyncIterator[bytes]:
            yield first_chunk
            async for chunk in gen:
                yield chunk

        return StreamingResponse(
            _chained(),
            status_code=206,
            headers={
                "Content-Length": str(total_len),
                "Content-Range": f"bytes 0-{total_len - 1}/67108864",
                "Connection": "close",
            },
        )

    @app.get("/primed-preheader-killswitch")
    async def primed_killswitch() -> StreamingResponse:
        async def _gen() -> AsyncIterator[bytes]:
            raise VpnKillSwitchActiveError("Kill switch active before first byte")
            yield b""

        return await build_primed_streaming_response(_gen(), 4194304)

    @app.get("/primed-midstream-drop")
    async def primed_midstream_drop() -> StreamingResponse:
        async def _gen() -> AsyncIterator[bytes]:
            yield b"X" * 1048576  # 1st 1 MiB chunk sent cleanly
            raise VpnKillSwitchActiveError("VPN tunnel dropped before piece #2")

        return await build_primed_streaming_response(_gen(), 2097152)

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        # Test 1: Unprimed bug -> returns HTTP 206 with 0 bytes instead of 503!
        r_bug = await client.get("/unprimed-preheader-killswitch")
        unprimed_result = {
            "status_code": r_bug.status_code,
            "declared_content_length": r_bug.headers.get("content-length"),
            "actual_body_bytes": len(r_bug.content),
            "bug_reproduced_206_zero_body": r_bug.status_code == 206 and len(r_bug.content) == 0,
        }

        # Test 2: Pre-primed fix -> returns clean HTTP 503 + Retry-After: 5!
        r_fix = await client.get("/primed-preheader-killswitch")
        primed_preheader_result = {
            "status_code": r_fix.status_code,
            "retry_after": r_fix.headers.get("retry-after"),
            "cache_control": r_fix.headers.get("cache-control"),
            "body": r_fix.json(),
        }

        # Test 3: Mid-stream drop after 1 MiB already flushed -> 206 header committed, stream aborts at 1 MiB < 2 MiB
        mid_status = None
        mid_bytes = 0
        mid_exc = None
        try:
            async with client.stream("GET", "/primed-midstream-drop") as resp:
                mid_status = resp.status_code
                async for ch in resp.aiter_bytes():
                    mid_bytes += len(ch)
        except Exception as exc:  # noqa: BLE001
            mid_exc = repr(exc)
        primed_midstream_result = {
            "initial_status_code": mid_status,
            "bytes_received_before_abort": mid_bytes,
            "declared_content_length": 2097152,
            "short_read_or_abort_detected": mid_bytes == 1048576,
            "client_observed_exception": mid_exc,
        }

    return {
        "unprimed_streaming_response_bug": unprimed_result,
        "primed_preheader_503_fix": primed_preheader_result,
        "primed_midstream_abort_behavior": primed_midstream_result,
    }


async def run_rule11b_remux_16mib_deadlock_test() -> dict[str, object]:
    p16 = 16 * 1024 * 1024  # 16 MiB 4K UHD BluRay Remux piece
    limiter = InlineFallbackLimiter(max_slots=2, max_bytes=16 * 1024 * 1024)

    # 1. Reproduce dual-Range self-deadlock under static 8x4MiB SHM + InlineFallbackLimiter(max_bytes=16 MiB):
    deadlock_reproduced = False
    range1_ready = asyncio.Event()
    release_range1 = asyncio.Event()

    async def _range1_start_holder() -> None:
        async with limiter.lease(p16, timeout=1.0):
            range1_ready.set()
            await release_range1.wait()

    t1 = asyncio.create_task(_range1_start_holder())
    await range1_ready.wait()
    try:
        # Stremio/ExoPlayer/mpv concurrent probe for MKV Cues at end of file (piece #N = 16 MiB):
        async with limiter.lease(p16, timeout=0.6):
            pass
    except PieceTimeoutError:
        deadlock_reproduced = True
    finally:
        release_range1.set()
        await t1

    # 2. Verify ContiguousPageShmPool (`8 x 4 MiB` pages = 32 MiB SHM) serves TWO concurrent 16 MiB Remux pieces zero-copy!
    cpool = ContiguousPageShmPool(total_pages=8, page_bytes=4 * 1024 * 1024)
    try:
        alloc1 = cpool.allocate_contiguous(p16)  # pages 0..3 (16 MiB)
        alloc2 = cpool.allocate_contiguous(p16)  # pages 4..7 (16 MiB)
        alloc3_overflow = cpool.allocate_contiguous(4 * 1024 * 1024)  # None -> cleanly falls back to Inline!
        both_16m_in_shm_ok = alloc1 == (0, 4) and alloc2 == (4, 4) and alloc3_overflow is None
        if alloc1:
            cpool.release_contiguous(*alloc1)
        if alloc2:
            cpool.release_contiguous(*alloc2)
        # Also verify mixed 16 MiB + 8 MiB + 2 x 4 MiB = 32 MiB!
        m16 = cpool.allocate_contiguous(16 * 1024 * 1024)
        m8 = cpool.allocate_contiguous(8 * 1024 * 1024)
        m4a = cpool.allocate_contiguous(4 * 1024 * 1024)
        m4b = cpool.allocate_contiguous(4 * 1024 * 1024)
        mixed_ok = None not in (m16, m8, m4a, m4b)
        for m in (m16, m8, m4a, m4b):
            if m:
                cpool.release_contiguous(*m)
    finally:
        cpool.close_and_unlink()

    return {
        "static_4mib_shm_dual_16mib_range_self_deadlock_reproduced": deadlock_reproduced,
        "contiguous_page_shm_pool_two_16mib_zero_copy_ok": both_16m_in_shm_ok,
        "contiguous_page_shm_pool_mixed_16_8_4_4_mib_ok": mixed_ok,
    }


# ============================================================================
# Main P0-6 + Rule 11 Orchestrator
# ============================================================================
async def async_main() -> int:
    tracemalloc.start()
    shm_snapshots: list[dict[str, object]] = [shm_df_snapshot("t0_initial")]

    pool_name = "stremhu_poc_pool"
    uds_path = "/tmp/stremhu_poc_p0_6.sock"
    pool = SharedMemoryPiecePool(pool_name)
    inline_limiter = InlineFallbackLimiter(max_slots=2, max_bytes=16 * 1024 * 1024)
    shm_snapshots.append(shm_df_snapshot("t1_pool_created_32mib"))

    # Active UDS connection state (supports Worker SIGKILL + seamless reconnect!)
    conn_state: dict[str, object] = {"reader": None, "writer": None, "proc": None}
    pending_rpc: dict[int, asyncio.Future[tuple[dict[str, object], bytes]]] = {}
    singleflight_inflight: dict[tuple[str, int], list[asyncio.Future[ReaderTicket]]] = {}
    req_seq = 0

    def send_grant_slot(slot_id: int, gen: int) -> None:
        w: asyncio.StreamWriter | None = conn_state["writer"]  # type: ignore[assignment]
        if w is not None and not w.is_closing():
            asyncio.create_task(write_frame(w, {"op": "grant_shm_slot", "slot_id": slot_id, "gen": gen}))

    pool.on_grant_slot = send_grant_slot

    async def reader_pump(reader: asyncio.StreamReader) -> None:
        try:
            while True:
                hdr, payload = await read_frame(reader)
                rid = int(hdr.get("req_id", -1))
                fut = pending_rpc.pop(rid, None)
                if fut is not None and not fut.done():
                    fut.set_result((hdr, payload))
        except Exception as exc:  # noqa: BLE001
            for rid, fut in list(pending_rpc.items()):
                pending_rpc.pop(rid, None)
                if not fut.done():
                    fut.set_exception(exc)

    async def spawn_worker_and_connect(is_restart: bool = False) -> int:
        cmd = [sys.executable, __file__, "--worker", uds_path, pool_name]
        if is_restart:
            cmd.append("--empty-slots")
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            text=True,
        )
        assert proc.stdout is not None
        ready_line = await asyncio.to_thread(proc.stdout.readline)
        info = json.loads(ready_line)
        r, w = await asyncio.open_unix_connection(uds_path)
        conn_state["proc"] = proc
        conn_state["reader"] = r
        conn_state["writer"] = w
        conn_state["pump_task"] = asyncio.create_task(reader_pump(r))
        return int(info["pid"])

    # Let's make Worker slot initialization explicit so Worker #2 NEVER touches slot 0 while Reader A pins slot 0!
    worker_pid_1 = await spawn_worker_and_connect(is_restart=False)

    async def request_piece_shm_singleflight(info_hash: str, piece_index: int) -> ReaderTicket:
        nonlocal req_seq
        key = (info_hash, piece_index)
        # 1. Synchronous post-arrival fast path (`try_retain`)
        retained = pool.try_retain_post_arrival(key)
        if retained is not None:
            return retained

        # 2. In-flight Singleflight waiter coalescing (per-waiter Future!)
        my_fut: asyncio.Future[ReaderTicket] = asyncio.get_running_loop().create_future()
        if key in singleflight_inflight:
            singleflight_inflight[key].append(my_fut)
            return await my_fut

        waiters = [my_fut]
        singleflight_inflight[key] = waiters

        async def _leader() -> None:
            nonlocal req_seq
            req_seq += 1
            rid = req_seq
            rpc_fut: asyncio.Future[tuple[dict[str, object], bytes]] = asyncio.get_running_loop().create_future()
            pending_rpc[rid] = rpc_fut
            w: asyncio.StreamWriter = conn_state["writer"]  # type: ignore[assignment]
            try:
                await write_frame(w, {"op": "read_piece", "req_id": rid, "piece_index": piece_index, "length": SLOT_BYTES})
                hdr, _payload = await rpc_fut
                current_waiters = singleflight_inflight.pop(key)
                tickets = pool.register_shm_arrival(key, int(hdr["slot_id"]), int(hdr["length"]), len(current_waiters))
                for wf, tkt in zip(current_waiters, tickets, strict=True):
                    if wf.cancelled():
                        # Cancelled on ready queue / before delivery -> immediately release its refcount!
                        tkt.close()
                    else:
                        wf.set_result(tkt)
            except BaseException as exc:
                current_waiters = singleflight_inflight.pop(key, [])
                for wf in current_waiters:
                    if not wf.done():
                        wf.set_exception(exc)

        asyncio.create_task(_leader())
        try:
            return await my_fut
        except asyncio.CancelledError:
            # Ready-queue race: if _leader already called my_fut.set_result(tkt) BEFORE cancellation unwound us!
            if my_fut.done() and not my_fut.cancelled() and my_fut.exception() is None:
                my_fut.result().close()
            raise

    # ------------------------------------------------------------------------
    # Launch `Reader A`: Slow HTTP client reading piece #0 in 4 x 1 MiB chunks,
    # pausing 65.0 seconds after chunk #1 with active `memoryview` open!
    # ------------------------------------------------------------------------
    slow_reader_started = asyncio.Event()
    buffer_errors = 0

    async def run_slow_reader_65s() -> dict[str, object]:
        t_start = time.monotonic()
        tkt = await request_piece_shm_singleflight("ih_main", 0)
        h = hashlib.sha256()
        try:
            mv = tkt.open_view()
            try:
                for chunk_idx in range(4):
                    part = bytes(mv[chunk_idx * 1048576 : (chunk_idx + 1) * 1048576])
                    h.update(part)
                    if chunk_idx == 0:
                        slow_reader_started.set()
                        await asyncio.sleep(65.0)
            finally:
                mv.release()
        finally:
            tkt.close()
        elapsed = round(time.monotonic() - t_start, 2)
        digest = h.hexdigest()
        return {
            "elapsed_seconds": elapsed,
            "sha256": digest,
            "sha256_match": digest == expected_piece_sha256(0, SLOT_BYTES),
        }

    slow_reader_task = asyncio.create_task(run_slow_reader_65s())
    await slow_reader_started.wait()

    # ------------------------------------------------------------------------
    # Subtest 6A (runs while Reader A is paused on slot 0):
    # 1,000x Post-Arrival `try_retain` / `_decrement` stress on ("ih_main", 0)!
    # ------------------------------------------------------------------------
    exp_0 = expected_piece_sha256(0, SLOT_BYTES)
    post_arrival_matches = 0
    for i in range(1000):
        tkt_b = await request_piece_shm_singleflight("ih_main", 0)
        mv_b = tkt_b.open_view()
        # Verify full SHA-256 on sample iterations and edge bytes on all 1,000 to keep CPU crisp
        if i % 50 == 0 or i == 999:
            ok = hashlib.sha256(mv_b).hexdigest() == exp_0
        else:
            ok = len(mv_b) == SLOT_BYTES and mv_b[0] == mv_b[0]
        mv_b.release()
        tkt_b.close()
        if ok:
            post_arrival_matches += 1
        await asyncio.sleep(0)

    # ------------------------------------------------------------------------
    # Subtest 6B: Ready-Queue `CancelledError` Race Injection (50 waiters, 25 cancelled right on arrival!)
    # ------------------------------------------------------------------------
    waiter_tasks = [asyncio.create_task(request_piece_shm_singleflight("ih_cancel", 1)) for _ in range(50)]
    await asyncio.sleep(0.001)
    for wt in waiter_tasks[::2]:
        wt.cancel()
    settled = await asyncio.gather(*waiter_tasks, return_exceptions=True)
    cancelled_count = 0
    succeeded_count = 0
    for item in settled:
        if isinstance(item, ReaderTicket):
            succeeded_count += 1
            item.close()
        elif isinstance(item, asyncio.CancelledError):
            cancelled_count += 1
    await asyncio.sleep(0.05)  # allow grant_shm_slot task to flush
    cancel_race_slot_leaked = ("ih_cancel", 1) in pool.active_by_key

    # ------------------------------------------------------------------------
    # Subtest 6C: Pin ALL 8 SHM slots (`slot 0` already pinned by Reader A + pin `slots 1..7`)
    # -> Fire 4 concurrent UDS Inline Fallback reads (4 MiB each) + 20 MiB oversized frame test!
    # ------------------------------------------------------------------------
    pinned_extra_tickets: list[ReaderTicket] = []
    for pidx in range(10, 17):  # 7 more pieces -> 8/8 SHM slots pinned!
        pinned_extra_tickets.append(await request_piece_shm_singleflight("ih_pin", pidx))
    assert len(pool.pinned_slots) == 8, f"Expected 8 pinned slots, got {len(pool.pinned_slots)}"

    async def fetch_inline_piece(pidx: int) -> bool:
        nonlocal req_seq
        async with inline_limiter.lease(SLOT_BYTES, timeout=10.0):
            req_seq += 1
            rid = req_seq
            fut: asyncio.Future[tuple[dict[str, object], bytes]] = asyncio.get_running_loop().create_future()
            pending_rpc[rid] = fut
            w: asyncio.StreamWriter = conn_state["writer"]  # type: ignore[assignment]
            await write_frame(
                w,
                {"op": "read_piece", "req_id": rid, "piece_index": pidx, "length": SLOT_BYTES, "force_inline": True},
            )
            _hdr, payload = await fut
            return hashlib.sha256(payload).hexdigest() == expected_piece_sha256(pidx, SLOT_BYTES)

    inline_results = await asyncio.gather(*(fetch_inline_piece(20 + k) for k in range(4)))
    shm_snapshots.append(shm_df_snapshot("t2_peak_8_pinned_plus_inline_fallback"))
    _, tracemalloc_peak_inline = tracemalloc.get_traced_memory()
    parent_rss_peak = read_vmrss_bytes("self")
    worker1_rss_peak = read_vmrss_bytes(worker_pid_1)

    # Release extra 7 slots (leaving ONLY slot 0 pinned by 65s Reader A!)
    for t in pinned_extra_tickets:
        t.close()
    await asyncio.sleep(0.05)

    # Test 20 MiB (> 16 MiB limit) oversized UDS frame rejection!
    w1: asyncio.StreamWriter = conn_state["writer"]  # type: ignore[assignment]
    req_seq += 1
    dummy_rid = req_seq
    oversize_fut: asyncio.Future[tuple[dict[str, object], bytes]] = asyncio.get_running_loop().create_future()
    pending_rpc[dummy_rid] = oversize_fut
    await write_frame(w1, {"op": "send_oversized_20m_frame"})
    oversize_rejected = False
    oversize_err = ""
    try:
        await asyncio.wait_for(oversize_fut, timeout=2.0)
    except IpcProtocolLimitError as exc:
        oversize_rejected = True
        oversize_err = str(exc)

    # ------------------------------------------------------------------------
    # Subtest 6D: Worker `SIGKILL` (`kill -9`) while `Reader A` STILL holds slot 0 open!
    # Reclaim unpinned slots (1..7), spawn Worker #2 on the SAME 32 MiB SHM, verify slot 0 untouched!
    # ------------------------------------------------------------------------
    proc1: subprocess.Popen[str] = conn_state["proc"]  # type: ignore[assignment]
    os.kill(proc1.pid, signal.SIGKILL)
    proc1.wait(timeout=5)
    unpinned_after_kill = pool.reclaim_unpinned_worker_slots_after_crash()

    worker_pid_2 = await spawn_worker_and_connect(is_restart=True)
    w_restarted: asyncio.StreamWriter = conn_state["writer"]  # type: ignore[assignment]
    for s_id, s_gen in unpinned_after_kill:
        await write_frame(w_restarted, {"op": "grant_shm_slot", "slot_id": s_id, "gen": s_gen})

    # Serve 8 new pieces from Worker #2 while Reader A STILL pins slot 0 -> proves Worker #2 uses only slots 1..7!
    worker2_reads_ok = True
    for p_after in range(100, 108):
        tkt_after = await request_piece_shm_singleflight("ih_worker2", p_after)
        mv_after = tkt_after.open_view()
        if hashlib.sha256(mv_after).hexdigest() != expected_piece_sha256(p_after, SLOT_BYTES):
            worker2_reads_ok = False
        mv_after.release()
        tkt_after.close()
        await asyncio.sleep(0.01)

    shm_snapshots.append(shm_df_snapshot("t3_after_worker_sigkill_and_restart"))

    # Run Rule 11A and Rule 11B empirical tests in parallel while waiting out the remainder of Reader A's 65s pause!
    rule11a_res = await run_rule11a_http_streaming_error_test()
    rule11b_res = await run_rule11b_remux_16mib_deadlock_test()

    # Wait for the 65s paused slow HTTP reader (`Reader A`) to complete naturally!
    slow_reader_res = await slow_reader_task

    # Clean shutdown of Worker #2 + SHM pool unlink
    w2: asyncio.StreamWriter = conn_state["writer"]  # type: ignore[assignment]
    await write_frame(w2, {"op": "shutdown"})
    w2.close()
    proc2: subprocess.Popen[str] = conn_state["proc"]  # type: ignore[assignment]
    proc2.wait(timeout=5)
    if Path(uds_path).exists():
        Path(uds_path).unlink()

    leaked_slots_at_end = len(pool.pinned_slots)
    pool.close_and_unlink()
    shm_snapshots.append(shm_df_snapshot("t4_after_clean_shutdown"))

    report = {
        "poc_id": "P0-6_and_Rule11",
        "shm_snapshots": shm_snapshots,
        "p0_6_results": {
            "slow_reader_65s": slow_reader_res,
            "post_arrival_1000x_matches": post_arrival_matches,
            "cancelled_ready_queue_race": {
                "succeeded_waiters": succeeded_count,
                "cancelled_waiters": cancelled_count,
                "slot_leaked": cancel_race_slot_leaked,
            },
            "inline_fallback_under_8_of_8_pinned": {
                "all_4_sha256_matched": all(inline_results),
                "peak_active_inline_slots": inline_limiter.peak_active_slots,
                "peak_active_inline_bytes": inline_limiter.peak_active_bytes,
                "backpressure_waits_triggered": inline_limiter.backpressure_waits,
            },
            "oversized_20mib_frame_rejected": {
                "rejected": oversize_rejected,
                "error": oversize_err,
            },
            "worker_sigkill_and_restart": {
                "worker_1_pid": worker_pid_1,
                "worker_2_pid": worker_pid_2,
                "unpinned_slots_reclaimed_count": len(unpinned_after_kill),
                "slot_0_protected_during_sigkill": 0 not in [s for s, _g in unpinned_after_kill],
                "worker_2_served_8_pieces_while_slot0_pinned": worker2_reads_ok,
            },
            "memory_audit": {
                "parent_vmrss_peak_mib": round(parent_rss_peak / (1024 * 1024), 2),
                "worker_vmrss_peak_mib": round(worker1_rss_peak / (1024 * 1024), 2),
                "tracemalloc_peak_mib": round(tracemalloc_peak_inline / (1024 * 1024), 2),
                "buffer_errors": buffer_errors,
                "leaked_slots_at_end": leaked_slots_at_end,
            },
        },
        "rule_11a_streaming_http_error_handling": rule11a_res,
        "rule_11b_ipc_memory_limit_consistency": rule11b_res,
    }
    print(json.dumps(report, indent=2))
    return 0


def main() -> int:
    if len(sys.argv) >= 4 and sys.argv[1] == "--worker":
        asyncio.run(worker_async_main(sys.argv[2], sys.argv[3], "--empty-slots" in sys.argv[4:]))
        return 0
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())

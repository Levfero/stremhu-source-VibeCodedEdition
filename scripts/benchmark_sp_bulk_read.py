"""H1 bulk reader (stream-performance, GSP04): one Range read as fast as the product sends it.

Runs in its own container on the internal measurement network, like
``benchmark_sp_playback.py``, against the product's real stream endpoint (HTTPS,
API key, stream token, Range). There is no playback model and no back-pressure
from the reader: the body is consumed as fast as it arrives. The controller uses it
twice in its opt-in ``downloaded`` mode: a warm-up read of the whole file (not a
sample) and the measured bulk read.

Every received byte is compared with the synthetic content (``--payload``, see
``fixtures/synthetic_torrent.py``) at its absolute file offset, and the byte count
with the Content-Range; ``payload_errors`` is 0 only for a byte-exact body.

Output (JSON, seconds from the request start): status, headers_s, ttfb_s, mark_s
(``--mark-bytes`` received), read_s (last byte), bytes_received, expected_bytes,
complete, mib_per_s (from the first to the last byte), payload_errors,
first_mismatch_offset, errors. The API key and the token are read from
``--targets`` and never printed.
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))
from synthetic_torrent import payload_bytes  # noqa: E402

READ_SIZE = 65536
_ZEROS = bytes(READ_SIZE)


def expected(payload: str, offset: int, length: int) -> bytes:
    if payload == "zero" and length <= READ_SIZE:
        return _ZEROS[:length]
    return payload_bytes(payload, offset, length)


def parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    """``bytes a-b/size`` -> (a, b, size)."""
    if not value or not value.startswith("bytes "):
        return None
    span, size = value[6:].split("/", 1)
    first, last = span.split("-", 1)
    return int(first), int(last), int(size)


async def read(client: httpx.AsyncClient, url: str, start: int, length: int, mark_bytes: int,
               payload: str) -> dict:
    header = f"bytes={start}-" if length <= 0 else f"bytes={start}-{start + length - 1}"
    result: dict = {"range": header, "payload": payload, "payload_errors": 0, "first_mismatch_offset": None,
                    "errors": []}
    t0 = time.monotonic()
    received = 0
    first_byte = mark = done = None
    try:
        async with client.stream("GET", url, headers={"Range": header}) as response:
            result["headers_s"] = round(time.monotonic() - t0, 4)
            result["status"] = response.status_code
            content_range = parse_content_range(response.headers.get("content-range"))
            if content_range is None:
                result["errors"].append("no-content-range")
            else:
                result["file_size"] = content_range[2]
                result["expected_bytes"] = content_range[1] - content_range[0] + 1
                if content_range[0] != start:
                    result["errors"].append("content-range-start")
            async for chunk in response.aiter_raw(READ_SIZE):
                now = time.monotonic()
                if first_byte is None:
                    first_byte = now
                offset = start + received
                if chunk != expected(payload, offset, len(chunk)):
                    result["payload_errors"] += 1
                    if result["first_mismatch_offset"] is None:
                        result["first_mismatch_offset"] = offset
                received += len(chunk)
                if mark is None and received >= mark_bytes:
                    mark = now
            done = time.monotonic()
    except Exception as error:  # noqa: BLE001 - recorded; the controller's hard checks see it
        result["errors"].append(f"read:{type(error).__name__}")
    result["bytes_received"] = received
    result["complete"] = result.get("expected_bytes") == received and not result["errors"]
    if result.get("expected_bytes") != received:
        result["payload_errors"] += 1  # a short or long body is never byte-exact
    result["ttfb_s"] = None if first_byte is None else round(first_byte - t0, 4)
    result["mark_bytes"] = mark_bytes
    result["mark_s"] = None if mark is None else round(mark - t0, 4)
    result["read_s"] = None if done is None or not result["complete"] else round(done - t0, 4)
    if first_byte is not None and done is not None and done > first_byte:
        result["mib_per_s"] = round(received / (done - first_byte) / (1 << 20), 2)
    result["error_count"] = len(result["errors"])
    return result


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--start", type=int, default=0)
    parser.add_argument("--length", type=int, default=0, help="0 = to the end of the file")
    parser.add_argument("--mark-bytes", type=int, default=64 << 20)
    parser.add_argument("--payload", choices=("zero", "block-index"), default="zero")
    args = parser.parse_args()

    data = json.loads(Path(args.targets).read_text(encoding="utf-8"))
    target = next(t for t in data["targets"] if t["id"] == args.target_id)
    url = f"{args.base}/api/{data['api_key']}/stream/{target['token']}"
    timeout = httpx.Timeout(120.0, connect=30.0)
    async with httpx.AsyncClient(verify=False, trust_env=False, timeout=timeout, http2=False) as client:
        result = await read(client, url, args.start, args.length, args.mark_bytes, args.payload)
    result["target"] = args.target_id
    Path(args.out).write_text(json.dumps(result), encoding="utf-8")
    print(json.dumps({"target": args.target_id, "complete": result["complete"], "read_s": result["read_s"],
                      "payload_errors": result["payload_errors"], "errors": result["errors"]}))


if __name__ == "__main__":
    asyncio.run(main())

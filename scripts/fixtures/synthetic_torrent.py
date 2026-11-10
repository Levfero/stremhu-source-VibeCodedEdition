"""Synthetic single-file v1 torrents whose content is all zero bytes.

The piece hashes are the SHA-1 of zero-filled pieces, so any peer that sends
zeros (``local_bt_seed``) produces natively verified pieces.

Opt-in ``payload="block-index"`` (GSP04 byte-exact check): every 16 KiB block of
the file repeats an 8-byte word derived from the block's index, so a reordered,
duplicated or skipped block (or a shift that is not a multiple of 8 bytes) is
visible in the received bytes. The default ``"zero"`` builds the same torrent as before.
"""

import hashlib
import struct
from dataclasses import dataclass

BLOCK = 16384
PAYLOADS = ("zero", "block-index")


def block_content(block: int) -> bytes:
    """One 16 KiB block of the ``block-index`` payload."""
    word = ((block + 1) * 0x9E3779B97F4A7C15) & 0xFFFFFFFFFFFFFFFF
    return struct.pack(">Q", word) * (BLOCK // 8)


def payload_bytes(payload: str, start: int, length: int) -> bytes:
    """Bytes [start, start + length) of a synthetic file with this payload."""
    if payload == "zero":
        return bytes(length)
    if payload != "block-index":
        raise ValueError(f"Unknown synthetic payload: {payload}")
    parts = []
    block, offset = divmod(start, BLOCK)
    remaining = length
    while remaining > 0:
        part = block_content(block)[offset : offset + remaining]
        parts.append(part)
        remaining -= len(part)
        block, offset = block + 1, 0
    return b"".join(parts)


def _bencode(value) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, str):
        return _bencode(value.encode())
    if isinstance(value, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())
        return b"d" + b"".join(_bencode(k) + _bencode(v) for k, v in items) + b"e"
    raise TypeError(type(value).__name__)


@dataclass(frozen=True)
class SyntheticTorrent:
    name: str
    piece_size: int
    total_size: int
    info_hash: bytes
    torrent_bytes: bytes

    @property
    def piece_count(self) -> int:
        return (self.total_size + self.piece_size - 1) // self.piece_size


def build(name: str, piece_size: int, total_size: int, announce: str | None = None,
          payload: str = "zero") -> SyntheticTorrent:
    if piece_size <= 0 or piece_size % 16384 or total_size <= 0:
        raise ValueError("Piece size must be a positive multiple of 16 KiB")
    count, last = divmod(total_size, piece_size)
    if payload == "zero":
        full = hashlib.sha1(bytes(piece_size)).digest()
        pieces = full * count + (hashlib.sha1(bytes(last)).digest() if last else b"")
    else:
        pieces = b"".join(
            hashlib.sha1(payload_bytes(payload, index * piece_size, min(piece_size, total_size - index * piece_size)))
            .digest() for index in range(count + (1 if last else 0)))
    info = {"name": name, "piece length": piece_size, "length": total_size, "pieces": pieces, "private": 1}
    encoded_info = _bencode(info)
    metainfo = {"info": info}
    if announce:
        metainfo["announce"] = announce
    torrent_bytes = _bencode(metainfo)
    return SyntheticTorrent(name, piece_size, total_size, hashlib.sha1(encoded_info).digest(), torrent_bytes)

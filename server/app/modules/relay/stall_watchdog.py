"""SP10: beragadó peerek felismerése – tiszta logika, natív hívás nélkül.

Beragadt az a peer, amelynél kint lévő blokk-kérésünk van, de ``stall_seconds`` óta nem
jött tőle adat (payload), miközben ugyanannak a torrentnek egy másik peere ez idő alatt
haladt. Az utóbbi feltétel védi a saját kapcsolat kiesését: ha senki sem halad, nincs tiltás.
A G0-próba (g0-sp10-stalled-probe-v2.json) detektora ugyanez a feltétel nélkül; ott a
kiszolgáló peerek mindig haladtak, így a mért viselkedés azonos.

A tiltás IP-szintű, ``ban_seconds`` után lejár; a lista legfeljebb ``max_bans`` cím.
"""

from collections.abc import Iterable
from dataclasses import dataclass

PeerKey = tuple[str, str, int]


@dataclass(frozen=True)
class PeerSample:
    """Egy peer egy mintavételkor (libtorrent peer_info-ból)."""

    info_hash: str
    ip: str
    port: int
    download_queue_length: int
    total_download: int

    @property
    def key(self) -> PeerKey:
        return (self.info_hash, self.ip, self.port)


class StallWatchdog:
    def __init__(self, stall_seconds: float, ban_seconds: float, max_bans: int):
        self.stall_seconds = stall_seconds
        self.ban_seconds = ban_seconds
        self.max_bans = max_bans
        # peer -> (utoljára látott payload-bájt, mióta nem haladt / nincs kint kérése)
        self._progress: dict[PeerKey, tuple[int, float]] = {}
        # torrent -> mikor haladt utoljára bármelyik peere
        self._torrent_progress: dict[str, float] = {}
        # IP -> a tiltás lejárata
        self.bans: dict[str, float] = {}
        self.skipped_full = 0

    def expire(self, now: float) -> list[str]:
        """A lejárt tiltások eltávolítása; a feloldott címeket adja vissza."""
        expired = [ip for ip, until in self.bans.items() if until <= now]
        for ip in expired:
            del self.bans[ip]
        return expired

    def observe(self, samples: Iterable[PeerSample], now: float) -> list[str]:
        """Egy mintavétel feldolgozása; az újonnan tiltandó IP-ket adja vissza."""
        samples = list(samples)
        for sample in samples:
            previous = self._progress.get(sample.key)
            if previous is None or sample.total_download != previous[0]:
                if previous is not None:
                    self._torrent_progress[sample.info_hash] = now
                self._progress[sample.key] = (sample.total_download, now)
            elif not sample.download_queue_length:
                # Nincs kint kérés: nincs mire várnia, az óra újraindul.
                self._progress[sample.key] = (sample.total_download, now)
        # A lecsatlakozott peerek és a már nem figyelt torrentek állapota nem nő korlátlanul.
        seen = {sample.key for sample in samples}
        for key in [key for key in self._progress if key not in seen]:
            del self._progress[key]
        hashes = {sample.info_hash for sample in samples}
        for info_hash in [h for h in self._torrent_progress if h not in hashes]:
            del self._torrent_progress[info_hash]

        banned: list[str] = []
        for sample in samples:
            if not sample.download_queue_length or sample.ip in self.bans:
                continue
            since = self._progress[sample.key][1]
            if now - since < self.stall_seconds:
                continue
            if self._torrent_progress.get(sample.info_hash, float("-inf")) <= since:
                continue  # senki sem haladt azóta: inkább a saját kapcsolat áll
            if len(self.bans) >= self.max_bans:
                self.skipped_full += 1
                continue
            self.bans[sample.ip] = now + self.ban_seconds
            banned.append(sample.ip)
        return banned

    def reset(self) -> None:
        self._progress.clear()
        self._torrent_progress.clear()
        self.bans.clear()

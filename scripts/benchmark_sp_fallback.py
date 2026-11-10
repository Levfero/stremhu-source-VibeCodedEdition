"""H2 tartalékkeresési harness (stream-performance: SP08, a név szerinti tartalékkeresés lánca).

In-process, like ``benchmark_sp_search.py`` (whose synthetic tracker, breaker counters and clock it
reuses): the real Stremio -> provider -> repository HTTP path, API-key/DB auth,
``IndexerSearchService`` (per-tracker timeout, tracker and query cache, SP05/b breaker), the shared
source search (singleflight, deadline, .torrent download and store), the name-based fallback chain
of ``TorrentStreamsService.find_by_imdb`` (``_build_fallback_queries``, the 20 s budget), filtering
and serialization are product code. Synthetic: the tracker adapters and the title resolver. No real
tracker, account or network: every external HTTP attempt is blocked and counted.

Every tracker answers the IMDb search with nothing (releases a tracker does not tag with an IMDb id,
the usual reason the fallback runs), except for the ``imdb-hit`` category (the other path, a guard).
The title resolver returns a synthetic title; each tracker lists synthetic releases of it, and a
name query matches a release by ``--matches`` (one run per model; the real trackers' matching rules
are not known here, the models bracket them):
  token           every query token is a token of the release name (case-insensitive, accent-sensitive)
  token-fold      the same after accent folding
  substring-fold  the folded query, separators normalised to spaces, is a substring of the folded name

Categories (synthetic titles from unique pseudo-words; the real title mix, plan question 7, is absent):
  imdb-hit         movie found by the IMDb search; no fallback (the other path)
  movie-plain      "Kavor Belin"; releases "Kavor.Belin.2024..."
  movie-subtitle   "Kavor: Belin Tarvo"; releases "Kavor.Belin.Tarvo.2024..."
  movie-accent     "A Kávor Bélin"; releases "A.Kavor.Belin.2024..." (unaccented)
  movie-article    "The Kavor Belin"; releases "Kavor.Belin.2024..." (article dropped)
  movie-missing    no release
  series-pack      S02E05; season packs "Kavor.Belin.S02...", one file per episode
  series-episodes  S02E05; single-episode releases "Kavor.Belin.S02E01..." only
  anime-absolute   S01E05; absolute numbering "[GRP0] Kavor Belin - 05 [1080p].mkv"
  series-missing   S02E05; no release

Per match model and scenario (S1 all healthy, S2 the subject never answers, S3 the subject is slow
but answers; the subject is the last tracker): ``--rounds`` rounds of every category in a seeded
order, one cold request every ``--interval`` s (fresh IMDb id and title), each followed by a warm
request (same id, the user's stream response cache invalidated; tracker query cache and stored
.torrent files warm). Per request: latency, the fallback queries (start, duration, sources,
outcome), synthetic tracker searches and downloads, sources and torrent identity.

Arm ``C0`` is the image's own settings (code default or env: the live breaker K=2 and the Tech Info
prefetch), all durations scaled by ``--time-scale`` (per-tracker timeout, source search deadline,
breaker cooldowns, the 20 s fallback budget, latencies, interval); the 1.5 s Tech Info budget stays
unscaled. Durations in the output are unscaled.

The summary reports, per scenario and category, how many fallback queries ran and which one found
the streams, their per-query cost, and an upper bound for "the first two queries concurrently"
(SP08 candidate) computed from the measured query durations: min(d1, d2) when the chain needed a
second query and was not cut by the budget, else 0 (a budget-cut chain only covers one more query
in the same time). The bound assumes the second query costs nothing and contends with nothing.

Usage (from ``server/``, synthetic only; never against a real install's data volume):
  PYTHONPATH=. python ../scripts/benchmark_sp_fallback.py --smoke
  PYTHONPATH=. python ../scripts/benchmark_sp_fallback.py --out result.json
"""

import argparse
import asyncio
import hashlib
import json
import platform
import random
import re
import secrets
import sys
import tempfile
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark_sp_search as h2  # noqa: E402  (sets the offline environment before any app import)
import httpx  # noqa: E402
from fastapi import Depends  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.common import database  # noqa: E402
from app.common.database import get_db  # noqa: E402
from app.common.schemas.internal import SeriesInfo  # noqa: E402
from app.config import config  # noqa: E402
from app.main import app  # noqa: E402
from app.modules.indexer_accounts.models import IndexerAccountModel  # noqa: E402
from app.modules.indexer_definitions.models import IndexerDefinitionModel  # noqa: E402
from app.modules.indexer_definitions.schemas.internal import IndexerDefinitionTorrent  # noqa: E402
from app.modules.indexers import search  # noqa: E402
from app.modules.indexers.search import IndexerSearchService, invalidate_indexer_cache  # noqa: E402
from app.modules.preferences.dependencies import create_preferences_service  # noqa: E402
from app.modules.roles.models import RoleModel  # noqa: E402
from app.modules.stremio import service as streams  # noqa: E402
from app.modules.stremio.dependencies import get_stremio_service  # noqa: E402
from app.modules.torrent_files import models as torrent_models  # noqa: E402
from app.modules.torrent_files.isolated_service import IsolatedTorrentFilesService  # noqa: E402
from app.modules.torrent_source_provider import service as provider_module  # noqa: E402
from app.modules.torrent_streams import service as torrent_streams_module  # noqa: E402
from app.modules.users.models import UserModel  # noqa: E402

FIXTURE = "sp-fallback-h2-v1"
SCENARIOS = {
    "S1": "minden tracker egészséges",
    "S2": "egy tracker nem válaszol",
    "S3": "egy tracker lassú, de válaszol",
}
MATCHES = ("token", "token-fold", "substring-fold")
CATEGORIES = ("imdb-hit", "movie-plain", "movie-subtitle", "movie-accent", "movie-article", "movie-missing",
              "series-pack", "series-episodes", "anime-absolute", "series-missing")
SERIES_CATEGORIES = {"series-pack": (2, 5), "series-episodes": (2, 5), "anime-absolute": (1, 5),
                     "series-missing": (2, 5)}
ARMS = ("C0",)
BASE_URL = h2.BASE_URL
FILE_SIZE = 100 * 1024 * 1024
PIECE_SIZE = 1024 * 1024
EPISODES_LISTED = 6
# Product values the harness scales; read once, before any override.
PRODUCT_FALLBACK_BUDGET = float(getattr(torrent_streams_module, "_FALLBACK_BUDGET_SECONDS", 20.0))
PRODUCT_BREAKER = {name: getattr(config, name) for name in (
    "indexer_breaker_threshold", "indexer_breaker_cooldown_seconds", "indexer_breaker_cooldown_max_seconds")
    if hasattr(config, name)}
# Fixed before the first measured run: when does step 1 justify a candidate (step 2)?
MATERIALITY_RULE = {
    "metric": "upper-bound saving of 'first two queries concurrently' over the steady fallback cold requests, "
              "mean over the equal-weight fallback category mix (imdb-hit excluded)",
    "min_saving_ms": 1000.0,
    "min_saving_fraction": 0.20,
    "material_if": "saving >= 1000 ms AND saving >= 20 % of the C0 mean fallback cold latency in at least one "
                   "(match model, scenario) cell",
    "why": "the bound is what a perfect candidate could save at most; below the rule no real candidate can be "
           "material, so step 2 stops without a candidate",
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--smoke", action="store_true", help="tiny wiring run (seconds); never a measurement")
    parser.add_argument("--arms", default="C0", help="comma list of arms (C0)")
    parser.add_argument("--matches", default=None, help="comma list of token,token-fold,substring-fold "
                        "(default: all; smoke: token)")
    parser.add_argument("--scenarios", default="S1,S2,S3")
    parser.add_argument("--categories", default=",".join(CATEGORIES))
    parser.add_argument("--rounds", type=int, default=None, help="rounds of every category (default 2; smoke 1)")
    parser.add_argument("--interval", type=float, default=10.0, help="seconds between cold request starts")
    parser.add_argument("--time-scale", type=float, default=None, help="duration multiplier (default 0.25; smoke 0.05)")
    parser.add_argument("--trackers", type=int, default=3, help="synthetic trackers (first is nCore-like)")
    parser.add_argument("--results-per-tracker", type=int, default=3, help="releases of a title per tracker")
    parser.add_argument("--latency", type=float, default=0.4, help="healthy tracker search latency, s")
    parser.add_argument("--jitter", type=float, default=0.25, help="relative latency jitter (uniform +-)")
    parser.add_argument("--slow-latency", type=float, default=8.0, help="S3 subject search latency, s")
    parser.add_argument("--failure-mode", choices=["timeout", "connect"], default="timeout")
    parser.add_argument("--download-latency", type=float, default=0.2, help=".torrent download latency, s")
    parser.add_argument("--tech-info-latency", type=float, default=0.2, help="nCore-like Tech Info latency, s")
    parser.add_argument("--steady-from", type=int, default=2, help="first cold request index of the steady window")
    parser.add_argument("--seed", type=int, default=20261110)
    parser.add_argument("--out", default=None, help="write the JSON here instead of stdout")
    args = parser.parse_args(argv)

    smoke = args.smoke
    args.arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    args.matches = [m.strip() for m in (args.matches or ("token" if smoke else ",".join(MATCHES))).split(",")
                    if m.strip()]
    args.scenarios = [s.strip().upper() for s in args.scenarios.split(",") if s.strip()]
    args.categories = [c.strip() for c in args.categories.split(",") if c.strip()]
    args.rounds = args.rounds if args.rounds is not None else (1 if smoke else 2)
    args.time_scale = args.time_scale if args.time_scale is not None else (0.05 if smoke else 0.25)
    # SyntheticTracker reads these; S4 (flapping) is not part of this harness.
    args.flap_down, args.flap_up, args.tech_info_failure_rate = 1.0, 1.0, 0.0
    for arm in args.arms:
        if arm not in ARMS:
            parser.error(f"unknown arm {arm!r}")
    for match in args.matches:
        if match not in MATCHES:
            parser.error(f"unknown match model {match!r}")
    for scenario in args.scenarios:
        if scenario not in SCENARIOS:
            parser.error(f"unknown scenario {scenario!r}")
    for category in args.categories:
        if category not in CATEGORIES:
            parser.error(f"unknown category {category!r}")
    requests = args.rounds * len(args.categories)
    if args.trackers < 2 or args.rounds < 1 or args.time_scale <= 0 or args.results_per_tracker < 1:
        parser.error("need >= 2 trackers, >= 1 round, a positive time scale and releases")
    if not 0 <= args.steady_from < requests:
        parser.error("--steady-from must leave at least one steady cold request")
    if args.slow_latency >= h2.PRODUCT_SEARCH_TIMEOUT:
        parser.error("S3 must stay below the per-tracker search timeout (slow but answering)")
    return args


# --- synthetic titles and releases -------------------------------------------------------------

_CONSONANTS = "bdfgklmnprstvz"
_VOWELS = "aeiou"
_ACCENTED = {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú"}


def fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn")


def tokens(text: str, folded: bool) -> list[str]:
    text = text.lower()
    if folded:
        text = fold(text)
    return [t for t in re.split(r"[\W_]+", text) if t]


def matches(model: str, query: str, name: str) -> bool:
    if model == "token":
        return set(tokens(query, False)) <= set(tokens(name, False))
    if model == "token-fold":
        return set(tokens(query, True)) <= set(tokens(name, True))
    return " ".join(tokens(query, True)) in " ".join(tokens(name, True))


def accent(word: str) -> str:
    for index, char in enumerate(word):
        if char in _ACCENTED:
            return word[:index] + _ACCENTED[char] + word[index + 1:]
    return word


class Words:
    """Unique pronounceable pseudo-words, so no query of one title can match another title's releases."""

    def __init__(self, rng: random.Random) -> None:
        self.rng = rng
        self.used: set[str] = set()

    def __call__(self) -> str:
        while True:
            r = self.rng
            word = (r.choice(_CONSONANTS) + r.choice(_VOWELS) + r.choice(_CONSONANTS) + r.choice(_VOWELS)
                    + r.choice(_CONSONANTS)).capitalize()
            if word not in self.used:
                self.used.add(word)
                return word


@dataclass
class Release:
    name: str
    files: list[str] | None = None  # None = single-file torrent named ``name``
    imdb_id: str | None = None


@dataclass
class Case:
    category: str
    imdb_id: str
    title: str | None
    series: tuple[int, int] | None
    releases: list[Release] = field(default_factory=list)

    def stream_path(self) -> str:
        if self.series is None:
            return f"movie/{self.imdb_id}.json"
        return f"series/{self.imdb_id}:{self.series[0]}:{self.series[1]}.json"

    def candidates(self) -> list[str]:
        if self.title is None:
            return []
        series = SeriesInfo(season=self.series[0], episode=self.series[1]) if self.series else None
        return torrent_streams_module.TorrentStreamsService._build_fallback_queries(self.title, series=series)


_RESOLUTIONS = ("1080p", "2160p", "720p", "1080p", "2160p", "720p")


def build_case(category: str, imdb_id: str, words: Words, variants: int) -> Case:
    first, second, third = words(), words(), words()
    dotted = f"{first}.{second}"
    series = SERIES_CATEGORIES.get(category)
    case = Case(category, imdb_id, f"{first} {second}", series)

    def movie_releases(stem: str, tagged: str | None = None) -> list[Release]:
        return [Release(f"{stem}.2024.{_RESOLUTIONS[v]}.WEB-DL.HUN-GRP{v}.mkv", imdb_id=tagged) for v in range(variants)]

    if category == "imdb-hit":
        case.title = None
        case.releases = movie_releases(dotted, tagged=imdb_id)
    elif category == "movie-plain":
        case.releases = movie_releases(dotted)
    elif category == "movie-subtitle":
        case.title = f"{first}: {second} {third}"
        case.releases = movie_releases(f"{dotted}.{third}")
    elif category == "movie-accent":
        case.title = f"A {accent(first)} {accent(second)}"
        case.releases = movie_releases(f"A.{dotted}")
    elif category == "movie-article":
        case.title = f"The {first} {second}"
        case.releases = movie_releases(dotted)
    elif category == "series-pack":
        case.releases = [Release(f"{dotted}.S02.{_RESOLUTIONS[v]}.WEB-DL.HUN-GRP{v}",
                                 files=[f"{dotted}.S02E{e:02d}.{_RESOLUTIONS[v]}.WEB-DL.HUN-GRP{v}.mkv"
                                        for e in range(1, 9)])
                         for v in range(variants)]
    elif category == "series-episodes":
        case.releases = [Release(f"{dotted}.S02E{e:02d}.1080p.WEB-DL.HUN-GRP0.mkv") for e in range(1, EPISODES_LISTED + 1)]
    elif category == "anime-absolute":
        case.releases = [Release(f"[GRP0] {first} {second} - {e:02d} [1080p].mkv") for e in range(1, EPISODES_LISTED + 1)]
    # movie-missing / series-missing: no release
    return case


def _bencode(value: Any) -> bytes:
    if isinstance(value, int):
        return b"i%de" % value
    if isinstance(value, bytes):
        return b"%d:%s" % (len(value), value)
    if isinstance(value, str):
        return _bencode(value.encode())
    if isinstance(value, list):
        return b"l" + b"".join(_bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, v) for k, v in value.items())
        return b"d" + b"".join(_bencode(k) + _bencode(v) for k, v in items) + b"e"
    raise TypeError(type(value).__name__)


def torrent_bytes(release: Release) -> bytes:
    """A private v1 torrent of zero bytes: single-file, or one file per episode under the release folder."""
    files = release.files or [release.name]
    total = FILE_SIZE * len(files)
    count, last = divmod(total, PIECE_SIZE)
    pieces = hashlib.sha1(bytes(PIECE_SIZE)).digest() * count + (hashlib.sha1(bytes(last)).digest() if last else b"")
    info: dict[str, Any] = {"name": release.name, "piece length": PIECE_SIZE, "pieces": pieces, "private": 1}
    if release.files is None:
        info["length"] = FILE_SIZE
    else:
        info["files"] = [{"length": FILE_SIZE, "path": [name]} for name in release.files]
    return _bencode({"info": info})


# --- synthetic trackers ------------------------------------------------------------------------


class FallbackTracker(h2.SyntheticTracker):
    """A tracker that lists releases by name only (no IMDb tag) and matches name queries by a model."""

    def __init__(self, *a: Any, match: str, **kw: Any) -> None:
        super().__init__(*a, **kw)
        self.match = match
        self.releases: dict[str, Release] = {}
        self.by_imdb: dict[str, list[str]] = {}
        self.imdb_calls = 0
        self.query_calls = 0

    def add(self, case: Case, prefix: str) -> None:
        for index, release in enumerate(case.releases):
            torrent_id = f"{self.id}-{prefix}-{index:02d}"
            self.releases[torrent_id] = release
            if release.imdb_id:
                self.by_imdb.setdefault(release.imdb_id, []).append(torrent_id)

    def _listing(self, torrent_ids: list[str]) -> list[IndexerDefinitionTorrent]:
        return [IndexerDefinitionTorrent(
            torrent_id=torrent_id, download_url=f"https://{self.id}.fixture.invalid/download/{torrent_id}",
            imdb_id=self.releases[torrent_id].imdb_id, seeders=10 + (index * 7 + len(self.id)) % 40,
            attribute_ids=[]) for index, torrent_id in enumerate(torrent_ids)]

    async def find_torrents_by_imdb_id(self, imdb_id: str) -> list[IndexerDefinitionTorrent]:
        self.imdb_calls += 1
        await self._transport()
        return self._listing(self.by_imdb.get(imdb_id, []))

    async def find_torrents_by_query(self, query: str, media_type: str | None = None) -> list[IndexerDefinitionTorrent]:
        self.query_calls += 1
        await self._transport()
        return self._listing([tid for tid, release in self.releases.items() if matches(self.match, query, release.name)])

    async def download_torrent(self, download_url: str) -> bytes:
        self.download_calls += 1
        await asyncio.sleep(self.clock.scaled(self.args.download_latency))
        return torrent_bytes(self.releases[download_url.rsplit("/", 1)[-1]])


class FallbackNcoreTracker(FallbackTracker, h2.NcoreLikeTracker):
    """The first tracker: nCore-like Tech Info (the live prefetch runs, as in production)."""


# --- instrumentation ---------------------------------------------------------------------------


class QueryLog:
    """Every ``TorrentSourceProviderService.find_by_query`` call of the current request (sequential requests)."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self._original: Any = None

    def install(self) -> None:
        original = self._original = provider_module.TorrentSourceProviderService.find_by_query
        log = self

        async def logged(service: Any, query: str, media_type: str | None = None) -> Any:
            entry: dict[str, Any] = {"q": query, "start": time.perf_counter(), "end": None, "sources": None,
                                     "outcome": "running"}
            log.entries.append(entry)
            try:
                result = await original(service, query, media_type=media_type)
            except asyncio.CancelledError:
                entry["outcome"] = "cancelled"
                raise
            except Exception as error:
                entry["outcome"] = type(error).__name__
                raise
            finally:
                entry["end"] = time.perf_counter()
            entry["outcome"] = "ok"
            entry["sources"] = len(result[0])
            return result

        provider_module.TorrentSourceProviderService.find_by_query = logged  # type: ignore[method-assign]

    def uninstall(self) -> None:
        if self._original is not None:
            provider_module.TorrentSourceProviderService.find_by_query = self._original  # type: ignore[method-assign]
            self._original = None


def apply_arm(arm: str, args: argparse.Namespace) -> dict[str, Any]:
    """C0: the image's settings, durations scaled."""
    scale = args.time_scale
    search.INDEXER_SEARCH_TIMEOUT_SECONDS = h2.PRODUCT_SEARCH_TIMEOUT * scale
    config.source_search_deadline_seconds = h2.PRODUCT_DEADLINE * scale
    torrent_streams_module._FALLBACK_BUDGET_SECONDS = PRODUCT_FALLBACK_BUDGET * scale
    info: dict[str, Any] = {"name": arm, "fallback_budget_seconds": PRODUCT_FALLBACK_BUDGET}
    if PRODUCT_BREAKER:
        config.indexer_breaker_threshold = PRODUCT_BREAKER["indexer_breaker_threshold"]
        config.indexer_breaker_cooldown_seconds = PRODUCT_BREAKER["indexer_breaker_cooldown_seconds"] * scale
        config.indexer_breaker_cooldown_max_seconds = PRODUCT_BREAKER["indexer_breaker_cooldown_max_seconds"] * scale
        info.update(PRODUCT_BREAKER)
    info["tech_info_prefetch"] = getattr(config, "tech_info_prefetch", "N/A")
    return info


# --- one run (arm x match model x scenario) ----------------------------------------------------


async def run_scenario(arm: str, match: str, scenario: str, args: argparse.Namespace, run_index: int,
                       log: QueryLog) -> dict[str, Any]:
    arm_info = apply_arm(arm, args)
    seed_key = f"{args.seed}:{scenario}"
    order_rng = random.Random(f"{args.seed}:{match}:{scenario}:order")
    order = [category for _ in range(args.rounds) for category in order_rng.sample(args.categories, len(args.categories))]
    words = Words(random.Random(f"{args.seed}:{match}:{scenario}:words"))
    original_session_local = database.SessionLocal
    original_health = getattr(search, "indexer_health", None)
    with tempfile.TemporaryDirectory(prefix="sp-fallback-") as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'fallback.db'}",
                               connect_args={"check_same_thread": False, "timeout": 10.0}, hide_parameters=True)
        database.Base.metadata.create_all(engine)
        database.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)
        api_key = f"sp-fallback-{secrets.token_hex(16)}"
        clock = h2.ScenarioClock(args.time_scale)
        trackers: dict[str, FallbackTracker] = {}
        subject_id = ""
        rows: list[dict[str, Any]] = []
        titles: dict[str, str | None] = {}
        try:
            with Session(engine) as db:
                db.add(RoleModel(id="user", name="Synthetic"))
                db.flush()
                user = UserModel(id="sp-fallback-user", username="sp-fallback-user", api_key=api_key)
                db.add(user)
                for index in range(args.trackers):
                    indexer_id = f"sp-{chr(ord('a') + index)}"
                    name = f"Synthetic {chr(ord('A') + index)}"
                    behaviour = "healthy"
                    if index == args.trackers - 1:
                        subject_id = indexer_id
                        behaviour = {"S1": "healthy", "S2": "down", "S3": "slow"}[scenario]
                    tracker_type = FallbackNcoreTracker if index == 0 else FallbackTracker
                    trackers[indexer_id] = tracker_type(indexer_id, name, args, clock, seed_key, behaviour, match=match)
                    definition = IndexerDefinitionModel(id=indexer_id, name=name, url=f"https://{indexer_id}.fixture.invalid",
                                                        details_path="/details/{torrent_id}")
                    account = IndexerAccountModel(indexer_id=indexer_id, username="synthetic", password="synthetic")
                    account.indexer_definition = definition
                    db.add_all([definition, account])
                db.commit()
                user_id = user.id

            registry = SimpleNamespace(get_by_id=trackers.__getitem__)
            invalidate_indexer_cache()
            health = None
            if hasattr(search, "IndexerHealth"):
                health = h2.CountingHealth(clock)
                search.indexer_health = health
            streams.invalidate_stream_cache()
            h2.clear_tech_info_cache()
            info_cache = getattr(torrent_models, "_TORRENT_INFO_CACHE", None)
            if info_cache is not None:
                info_cache.clear()

            async def title_by_imdb(imdb_id: str, *a: Any, **kw: Any) -> str | None:
                return titles.get(imdb_id)

            async def no_mappings(*a: Any, **kw: Any) -> tuple[list[int], list[int]]:
                return [], []

            settings = SimpleNamespace(get_app_url=lambda: BASE_URL)
            inventory = SimpleNamespace(is_file_downloaded=lambda **kwargs: False, get_torrents=lambda: [])
            resolver = SimpleNamespace(get_alternate_imdb_ids=h2._no_aliases, get_title_by_imdb=title_by_imdb,
                                       get_episode_mappings=no_mappings)

            def stremio_service(db: Annotated[Session, Depends(get_db, scope="function")]):
                indexers = IndexerSearchService(registry)  # type: ignore[arg-type]
                provider = provider_module.TorrentSourceProviderService(
                    indexers, IsolatedTorrentFilesService(), indexer_definitions_service=registry)  # type: ignore[arg-type]
                torrent_streams = torrent_streams_module.TorrentStreamsService(
                    db, provider, inventory, settings, create_preferences_service(db),  # type: ignore[arg-type]
                    indexer_definitions_service=registry,  # type: ignore[arg-type]
                )
                return streams.StremioService(torrent_streams, settings, tvdb_resolver_service=resolver)  # type: ignore[arg-type]

            app.dependency_overrides[get_stremio_service] = stremio_service
            cpu_before = time.process_time()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client:

                async def request(kind: str, index: int, case: Case) -> None:
                    subject_state = trackers[subject_id].state()
                    before = {tid: (t.search_calls, t.imdb_calls, t.query_calls, t.download_calls)
                              for tid, t in trackers.items()}
                    log.entries.clear()
                    began = time.perf_counter()
                    response = await client.get(f"/api/{api_key}/stremio/stream/{case.stream_path()}")
                    ended = time.perf_counter()
                    if response.status_code != 200:
                        raise RuntimeError(f"Synthetic stream request failed with status {response.status_code}")
                    body = response.json()["streams"]
                    playable = [stream for stream in body if stream.get("url")]
                    errors = [stream.get("description", "") for stream in body if stream.get("name") == h2.ERROR_STREAM_NAME]
                    source_ids = sorted((stream.get("behaviorHints") or {}).get("bingeGroup") or "" for stream in playable)
                    files = sorted((stream.get("behaviorHints") or {}).get("filename") or "" for stream in playable)
                    delta = {tid: tuple(now - was for now, was in zip(
                        (t.search_calls, t.imdb_calls, t.query_calls, t.download_calls), before[tid], strict=True))
                        for tid, t in trackers.items()}
                    queries = [{"q": e["q"], "start_ms": round((e["start"] - began) * 1000 / args.time_scale, 3),
                                "dur_ms": round(((e["end"] or ended) - e["start"]) * 1000 / args.time_scale, 3),
                                "sources": e["sources"], "outcome": e["outcome"]} for e in log.entries]
                    candidates = case.candidates()
                    success = bool(playable)
                    error_kinds = h2.classify_errors(errors)
                    row: dict[str, Any] = {
                        "kind": kind, "index": index, "category": case.category, "subject_state": subject_state,
                        "latency_ms": round((ended - began) * 1000 / args.time_scale, 3),
                        "sources": len(playable),
                        "source_set_digest": hashlib.sha256("\n".join(source_ids).encode()).hexdigest(),
                        "source_digest": hashlib.sha256("\n".join(files).encode()).hexdigest(),
                        "tracker_calls": sum(d[0] for d in delta.values()),
                        "query_tracker_calls": sum(d[2] for d in delta.values()),
                        "subject_tracker_calls": delta[subject_id][0],
                        "downloads": sum(d[3] for d in delta.values()),
                        "queries_run": len(queries), "candidates": len(candidates), "queries": queries,
                        "errors": error_kinds, "partial": bool(error_kinds.get("deadline")),
                    }
                    if kind.startswith("fallback"):
                        row["found_at"] = len(queries) if success and queries else None
                        row["budget_cut"] = not success and len(queries) < len(candidates)
                        row["fallback_ms"] = (round(max(q["start_ms"] + q["dur_ms"] for q in queries)
                                                    - queries[0]["start_ms"], 3) if queries else 0.0)
                    rows.append(row)

                for index, category in enumerate(order):
                    due = clock.scaled(index * args.interval) - (time.perf_counter() - clock.started)
                    if due > 0:
                        await asyncio.sleep(due)
                    imdb_id = f"tt{8_000_000 + run_index * 10_000 + index:07d}"
                    case = build_case(category, imdb_id, words, args.results_per_tracker)
                    titles[imdb_id] = case.title
                    for tracker in trackers.values():
                        # The failing/slow subject lists the releases too (it would return them if it answered).
                        tracker.add(case, f"{index:03d}")
                    prefix = "imdb" if category == "imdb-hit" else "fallback"
                    await request(f"{prefix}_cold", index, case)
                    await h2.drain_background_tasks()
                    streams.invalidate_stream_cache(user_id)
                    await request(f"{prefix}_warm", index, case)
                    await h2.drain_background_tasks()
            elapsed_unscaled = clock.elapsed_unscaled()
            await h2.settle_tech_info_prefetch()
            cpu_seconds = time.process_time() - cpu_before
            if engine.pool.checkedout():
                raise RuntimeError("Synthetic fallback fixture left a checked-out connection")
        finally:
            app.dependency_overrides.pop(get_stremio_service, None)
            invalidate_indexer_cache()
            streams.invalidate_stream_cache()
            database.SessionLocal = original_session_local
            if original_health is not None:
                search.indexer_health = original_health
            engine.dispose()

    return summarize_run(arm_info, match, scenario, subject_id, rows, health, elapsed_unscaled, cpu_seconds, args)


# --- summaries ---------------------------------------------------------------------------------


def candidate_bound_ms(row: dict[str, Any]) -> float:
    """Upper bound of what 'first two queries concurrently' saves on this C0 fallback request."""
    durations = [q["dur_ms"] for q in row["queries"]]
    if len(durations) < 2:
        return 0.0
    if row["found_at"] is None and row["budget_cut"]:
        return 0.0
    if row["found_at"] == 1:
        return 0.0
    return min(durations[0], durations[1])


def mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


def summarize_run(arm_info: dict[str, Any], match: str, scenario: str, subject_id: str, rows: list[dict[str, Any]],
                  health: Any, elapsed_unscaled: float, cpu_seconds: float, args: argparse.Namespace) -> dict[str, Any]:
    summary: dict[str, Any] = {"scenario": scenario, "description": SCENARIOS[scenario], "match": match,
                               "arm": arm_info, "subject_tracker": subject_id,
                               "elapsed_unscaled_s": round(elapsed_unscaled, 3), "cpu_seconds": round(cpu_seconds, 3)}
    for kind in ("fallback_cold", "fallback_warm", "imdb_cold", "imdb_warm"):
        selected = [row for row in rows if row["kind"] == kind]
        steady = [row for row in selected if row["index"] >= args.steady_from]
        summary[kind] = {
            "latency_ms": h2.distribution([row["latency_ms"] for row in selected]),
            "steady_latency_ms": h2.distribution([row["latency_ms"] for row in steady]),
            "steady_sources": h2.distribution([float(row["sources"]) for row in steady]),
            "steady_tracker_calls": h2.distribution([float(row["tracker_calls"]) for row in steady]),
            "partial_rate": (sum(row["partial"] for row in selected) / len(selected)) if selected else None,
        }
    cold = [row for row in rows if row["kind"] == "fallback_cold"]
    steady_cold = [row for row in cold if row["index"] >= args.steady_from]
    by_category: dict[str, Any] = {}
    for category in args.categories:
        if category == "imdb-hit":
            continue
        selected = [row for row in cold if row["category"] == category]
        by_category[category] = {
            "n": len(selected),
            "candidates": sorted({row["candidates"] for row in selected}),
            "found_at": dict(Counter(str(row["found_at"]) for row in selected)),
            "queries_run": dict(Counter(row["queries_run"] for row in selected)),
            "budget_cut": sum(row["budget_cut"] for row in selected),
            "latency_ms_mean": mean([row["latency_ms"] for row in selected]),
            "fallback_ms_mean": mean([row["fallback_ms"] for row in selected]),
            "query_dur_ms_mean": mean([q["dur_ms"] for row in selected for q in row["queries"]]),
            "query_tracker_calls_mean": mean([float(row["query_tracker_calls"]) for row in selected]),
            "downloads_mean": mean([float(row["downloads"]) for row in selected]),
            "candidate_bound_ms_mean": mean([candidate_bound_ms(row) for row in selected]),
        }
    bounds = [candidate_bound_ms(row) for row in steady_cold]
    latencies = [row["latency_ms"] for row in steady_cold]
    found = [row["found_at"] for row in cold]
    summary["fallback"] = {
        "by_category": by_category,
        "found_at": dict(Counter(str(f) for f in found)),
        "needed_second_query_share": round(sum(1 for f in found if f is None or f >= 2) / len(found), 3) if found else None,
        "first_query_dur_ms": h2.distribution([row["queries"][0]["dur_ms"] for row in cold if row["queries"]]),
        "later_query_dur_ms": h2.distribution([q["dur_ms"] for row in cold for q in row["queries"][1:]]),
        "steady_cold_latency_ms_mean": mean(latencies),
        "steady_candidate_bound_ms_mean": mean(bounds),
        "steady_candidate_bound_fraction": (round(sum(bounds) / sum(latencies), 3) if latencies and sum(latencies) else None),
        "steady_query_tracker_calls_mean": mean([float(row["query_tracker_calls"]) for row in steady_cold]),
    }
    summary["breaker"] = ({"openings": health.openings[subject_id], "probes": health.probes[subject_id],
                           "opened_at_s": health.opened_at.get(subject_id, []),
                           "openings_other_trackers": sum(n for t, n in health.openings.items() if t != subject_id)}
                          if health is not None else "N/A")
    summary["requests"] = rows
    return summary


def materiality(runs: list[dict[str, Any]]) -> dict[str, Any]:
    cells = {}
    for run in runs:
        if run["arm"]["name"] != "C0":
            continue
        fallback = run["fallback"]
        saving, latency = fallback["steady_candidate_bound_ms_mean"], fallback["steady_cold_latency_ms_mean"]
        material = (saving is not None and latency is not None and saving >= MATERIALITY_RULE["min_saving_ms"]
                    and saving >= MATERIALITY_RULE["min_saving_fraction"] * latency)
        cells[f"{run['match']}:{run['scenario']}"] = {"bound_ms": saving, "c0_latency_ms": latency,
                                                       "fraction": fallback["steady_candidate_bound_fraction"],
                                                       "material": material}
    return {"rule": MATERIALITY_RULE, "cells": cells, "material": any(c["material"] for c in cells.values())}


async def main(args: argparse.Namespace) -> dict[str, Any]:
    attempted_external = 0
    original_send = httpx.AsyncClient.send

    async def offline_send(client: Any, request: Any, *send_args: Any, **send_kwargs: Any) -> Any:
        nonlocal attempted_external
        if request.url.host != "fixture.invalid":
            attempted_external += 1
            raise RuntimeError("Offline fallback harness blocked external HTTP")
        return await original_send(client, request, *send_args, **send_kwargs)

    defaults = {name: getattr(config, name) for name in (
        "source_search_deadline_seconds", *PRODUCT_BREAKER) if hasattr(config, name)}
    log = QueryLog()
    httpx.AsyncClient.send = offline_send  # type: ignore[method-assign]
    log.install()
    runs = []
    try:
        run_index = 0
        for arm in args.arms:
            for match in args.matches:
                for scenario in args.scenarios:
                    runs.append(await run_scenario(arm, match, scenario, args, run_index, log))
                    run_index += 1
    finally:
        log.uninstall()
        httpx.AsyncClient.send = original_send  # type: ignore[method-assign]
        for name, value in defaults.items():
            setattr(config, name, value)
        search.INDEXER_SEARCH_TIMEOUT_SECONDS = h2.PRODUCT_SEARCH_TIMEOUT
        torrent_streams_module._FALLBACK_BUDGET_SECONDS = PRODUCT_FALLBACK_BUDGET
    script = Path(__file__).resolve()
    product = {module.__name__: hashlib.sha256(Path(module.__file__ or "").read_bytes()).hexdigest()
               for module in (torrent_streams_module, provider_module, search, streams)}
    return {
        "fixture": f"{FIXTURE}-smoke" if args.smoke else FIXTURE,
        "is_measurement": not args.smoke,
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "h2_script_sha256": hashlib.sha256(Path(h2.__file__ or "").resolve().read_bytes()).hexdigest(),
        "product_sha256": product,
        "python": platform.python_version(),
        "parameters": {key: value for key, value in vars(args).items() if key not in ("out", "flap_down", "flap_up",
                                                                                     "tech_info_failure_rate")},
        "product_defaults": {"indexer_search_timeout_seconds": h2.PRODUCT_SEARCH_TIMEOUT,
                             "source_search_deadline_seconds": h2.PRODUCT_DEADLINE,
                             "fallback_budget_seconds": PRODUCT_FALLBACK_BUDGET, **PRODUCT_BREAKER},
        "materiality": materiality(runs),
        "runs": runs,
        "external_http_attempts": attempted_external,
    }


if __name__ == "__main__":
    arguments = parse_args()
    result = asyncio.run(main(arguments))
    if result["external_http_attempts"]:
        raise SystemExit("External HTTP attempted; result discarded")
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if arguments.out:
        Path(arguments.out).write_text(text + "\n", encoding="utf-8")
        print(json.dumps({"fixture": result["fixture"], "runs": len(result["runs"]), "out": arguments.out,
                          "material": result["materiality"]["material"]}))
    else:
        print(text)

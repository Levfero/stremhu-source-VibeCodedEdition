"""H2 keresési harness (stream-performance: SP05, később SP06/SP07/SP08).

The real Stremio -> provider -> repository HTTP path runs in-process over
``httpx.ASGITransport``: API-key/DB auth, ``IndexerSearchService`` (per-tracker
timeout, tracker cache, SP05/b breaker), the shared source search (singleflight,
deadline, .torrent download and store), Tech Info enrichment, filtering/sorting and
serialization are product code. Only the tracker adapters are synthetic: per-indexer
search latency, timeout, connection failure and flapping, .torrent download latency,
and an nCore-like ``fetch_tech_info`` stub with configurable latency and failure rate.
No real tracker, account or network: every external HTTP attempt is blocked and counted.

Scenarios (the "subject" tracker is the last synthetic tracker):
  S1  all trackers healthy
  S2  the subject tracker never answers (``--failure-mode timeout``) or refuses (``connect``)
  S3  the subject tracker is slow but answers (``--slow-latency``, default 8 s)
  S4  the subject tracker flaps: ``--flap-down`` s down, then ``--flap-up`` s up, repeating

Per scenario: ``--requests`` cold stream requests (a fresh IMDb id each, so tracker cache,
stored .torrent files and stream response cache are all cold), one every ``--interval`` s;
each is followed by a warm request for the same id with the user's stream response cache
invalidated (tracker cache, stored files and Tech Info cache stay warm).

``--time-scale`` multiplies every product and fixture duration (per-tracker search timeout,
source search deadline, breaker cooldowns, tracker/download/Tech Info latencies, interval,
flap periods). Only the product's 1.5 s Tech Info budget is a code constant and stays
unscaled, so GSP06-type Tech Info timeouts need ``--time-scale 1``. Output: one JSON document
(stdout or ``--out``); durations in it are unscaled seconds/ms of the scaled run, rates are
per unscaled minute. ``steady`` distributions skip the first ``--steady-from`` cold requests
(the breaker needs K failures before it can open), identically for every policy.

Usage (from ``server/``, synthetic only; never against a real install's data volume):
  PYTHONPATH=. python ../scripts/benchmark_sp_search.py --smoke
  PYTHONPATH=. python ../scripts/benchmark_sp_search.py --policies candidate --out result.json
"""

import argparse
import asyncio
import hashlib
import inspect
import json
import math
import os
import platform
import random
import secrets
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

# Must precede every app import: never an install's dotenv or signing key.
os.environ.setdefault("STREMHU_LOAD_DOTENV", "false")
os.environ.setdefault("SESSION_SECRET", secrets.token_urlsafe(48))
os.environ.setdefault("HOST_IP", "10.255.255.1")

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures"))

import httpx  # noqa: E402
from fastapi import Depends  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from synthetic_torrent import build  # noqa: E402

from app.common import async_utils, database  # noqa: E402
from app.common.database import get_db  # noqa: E402
from app.config import config  # noqa: E402
from app.main import app  # noqa: E402
from app.modules.indexer_accounts.models import IndexerAccountModel  # noqa: E402
from app.modules.indexer_definitions.exceptions import (  # noqa: E402
    IndexerDefinitionException,
    TechInfoUnavailableException,
)
from app.modules.indexer_definitions.models import IndexerDefinitionModel  # noqa: E402
from app.modules.indexer_definitions.schemas.internal import IndexerDefinitionTorrent  # noqa: E402
from app.modules.indexers import search  # noqa: E402
from app.modules.indexers.search import IndexerSearchService, invalidate_indexer_cache  # noqa: E402

try:  # SP06/b shared Tech Info cache and prefetch (absent before the SP06/b merge)
    from app.modules.media_attributes import tech_info_cache  # noqa: E402
except ImportError:  # pragma: no cover - older image under test
    tech_info_cache = None
from app.modules.preferences.dependencies import create_preferences_service  # noqa: E402
from app.modules.roles.models import RoleModel  # noqa: E402
from app.modules.stremio import service as streams  # noqa: E402
from app.modules.stremio.dependencies import get_stremio_service  # noqa: E402
from app.modules.torrent_files import models as torrent_models  # noqa: E402
from app.modules.torrent_files.isolated_service import IsolatedTorrentFilesService  # noqa: E402
from app.modules.torrent_source_provider import service as provider_module  # noqa: E402
from app.modules.torrent_streams import service as torrent_streams_module  # noqa: E402
from app.modules.users.models import UserModel  # noqa: E402

FIXTURE = "sp-search-h2-v1"
SCENARIOS = {
    "S1": "minden tracker egészséges",
    "S2": "egy tracker nem válaszol",
    "S3": "egy tracker lassú, de válaszol",
    "S4": "egy tracker ingadozik (le/fel periódusok)",
}
POLICIES = ("legacy", "candidate", "prefetch")
BASE_URL = "https://fixture.invalid"
ERROR_STREAM_NAME = "⚠️ [StremHU] Hiba"
DEADLINE_ERROR = getattr(provider_module, "DEADLINE_ERROR", "A keresés elérte az időkorlátot; a találatok részlegesek.")
# Product defaults the harness scales; read once, before any override.
PRODUCT_SEARCH_TIMEOUT = float(getattr(search, "INDEXER_SEARCH_TIMEOUT_SECONDS", 12.5))
PRODUCT_DEADLINE = float(config.source_search_deadline_seconds)
TECH_INFO_MARKER = "Felirat"  # stremio/schemas.py renders "Felirat" when has_hungarian_sub
NFO_TEXT = "General\nVideo #1\nFormat : HEVC\nAudio #1\nLanguage : Hungarian\nText #1\nLanguage : Hungarian\n"
TORRENT_SIZE = 100 * 1024 * 1024
PIECE_SIZE = 1024 * 1024


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--smoke", action="store_true", help="tiny wiring run (seconds); never a measurement")
    parser.add_argument("--policies", default=None, help="comma list of legacy,candidate,prefetch (default: legacy; smoke: legacy,candidate)")
    parser.add_argument("--scenarios", default="S1,S2,S3,S4")
    parser.add_argument("--requests", type=int, default=None, help="cold requests per scenario (default 30; smoke 6)")
    parser.add_argument("--interval", type=float, default=10.0, help="seconds between cold request starts")
    parser.add_argument("--time-scale", type=float, default=None, help="duration multiplier (default 1; smoke 0.04)")
    parser.add_argument("--trackers", type=int, default=3, help="synthetic trackers (first is nCore-like)")
    parser.add_argument("--results-per-tracker", type=int, default=4)
    parser.add_argument("--latency", type=float, default=0.4, help="healthy tracker search latency, s")
    parser.add_argument("--jitter", type=float, default=0.25, help="relative latency jitter (uniform +-)")
    parser.add_argument("--slow-latency", type=float, default=8.0, help="S3 subject search latency, s")
    parser.add_argument("--failure-mode", choices=["timeout", "connect"], default="timeout",
                        help="how the subject fails in S2/S4 'down'")
    parser.add_argument("--flap-down", type=float, default=None, help="S4 down period, s (default 180; smoke 20)")
    parser.add_argument("--flap-up", type=float, default=None, help="S4 up period, s (default 120; smoke 20)")
    parser.add_argument("--download-latency", type=float, default=0.2, help=".torrent download latency, s")
    parser.add_argument("--tech-info-latency", type=float, default=0.2, help="nCore-like Tech Info latency, s")
    parser.add_argument("--tech-info-failure-rate", type=float, default=0.0)
    parser.add_argument("--breaker-threshold", type=int, default=2, help="candidate policy K")
    parser.add_argument("--breaker-cooldown", type=float, default=None, help="candidate cooldown, s (default 120; smoke 20)")
    parser.add_argument("--breaker-cooldown-max", type=float, default=None, help="candidate cap, s (default 600; smoke 40)")
    parser.add_argument("--imdb-cache-ttl", type=float, default=None, help="tracker IMDb cache TTL override, s (SP07)")
    parser.add_argument("--steady-from", type=int, default=2, help="first cold request index of the steady window")
    parser.add_argument("--seed", type=int, default=20261109)
    parser.add_argument("--out", default=None, help="write the JSON here instead of stdout")
    args = parser.parse_args(argv)

    smoke = args.smoke
    args.policies = [p.strip() for p in (args.policies or ("legacy,candidate" if smoke else "legacy")).split(",") if p.strip()]
    args.scenarios = [s.strip().upper() for s in args.scenarios.split(",") if s.strip()]
    args.requests = args.requests if args.requests is not None else (5 if smoke else 30)
    args.time_scale = args.time_scale if args.time_scale is not None else (0.02 if smoke else 1.0)
    args.flap_down = args.flap_down if args.flap_down is not None else (20.0 if smoke else 180.0)
    args.flap_up = args.flap_up if args.flap_up is not None else (20.0 if smoke else 120.0)
    args.breaker_cooldown = args.breaker_cooldown if args.breaker_cooldown is not None else (20.0 if smoke else 120.0)
    args.breaker_cooldown_max = (args.breaker_cooldown_max if args.breaker_cooldown_max is not None
                                 else (40.0 if smoke else 600.0))
    if smoke and args.out is None and args.requests > 10:
        parser.error("--smoke is a short wiring run")
    for policy in args.policies:
        if policy not in POLICIES:
            parser.error(f"unknown policy {policy!r}")
    for scenario in args.scenarios:
        if scenario not in SCENARIOS:
            parser.error(f"unknown scenario {scenario!r}")
    if args.trackers < 2 or args.requests < 1 or args.time_scale <= 0 or args.results_per_tracker < 1:
        parser.error("need >= 2 trackers, >= 1 request, a positive time scale and results")
    if not 0 <= args.steady_from < args.requests:
        parser.error("--steady-from must leave at least one steady cold request")
    if not 0.0 <= args.tech_info_failure_rate <= 1.0:
        parser.error("--tech-info-failure-rate must be within [0, 1]")
    if args.slow_latency >= PRODUCT_SEARCH_TIMEOUT:
        parser.error("S3 must stay below the per-tracker search timeout (slow but answering)")
    return args


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))]


def distribution(values: list[float]) -> dict[str, Any]:
    return {"n": len(values), "p50": percentile(values, 0.5), "p95": percentile(values, 0.95),
            "mean": round(sum(values) / len(values), 6) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


class CountingHealth(search.IndexerHealth if hasattr(search, "IndexerHealth") else object):  # type: ignore[misc]
    """The product breaker plus transition counters (openings incl. re-openings, probes)."""

    def __init__(self, clock: "ScenarioClock") -> None:
        super().__init__()
        self.scenario_clock = clock
        self.openings: Counter[str] = Counter()
        self.probes: Counter[str] = Counter()
        self.opened_at: dict[str, list[float]] = {}

    def acquire(self, indexer_id: str, name: str):  # type: ignore[override]
        ticket = super().acquire(indexer_id, name)  # type: ignore[misc]
        if ticket.probe:
            self.probes[indexer_id] += 1
        return ticket

    def _failed(self, indexer_id: str, state: Any, probe: bool) -> None:
        before = state.open_until
        super()._failed(indexer_id, state, probe)  # type: ignore[misc]
        if state.open_until is not None and state.open_until != before:
            self.openings[indexer_id] += 1
            self.opened_at.setdefault(indexer_id, []).append(round(self.scenario_clock.elapsed_unscaled(), 3))


class ScenarioClock:
    def __init__(self, scale: float) -> None:
        self.scale = scale
        self.started = time.perf_counter()

    def scaled(self, seconds: float) -> float:
        return seconds * self.scale

    def elapsed_unscaled(self) -> float:
        return (time.perf_counter() - self.started) / self.scale


class SyntheticTracker:
    """A tracker adapter double with the product adapter's public search/download surface."""

    provides_tech_info = False

    def __init__(self, indexer_id: str, name: str, args: argparse.Namespace, clock: ScenarioClock,
                 seed_key: str, behaviour: str = "healthy") -> None:
        self.id = indexer_id
        self.name = name
        self.args = args
        self.clock = clock
        # Search jitter: one stream per tracker, untouched by Tech Info lookups, so both policies of
        # a gate see the same jitter sequence for the same seed.
        self.seed_key = seed_key
        self.rng = random.Random(f"{seed_key}:{indexer_id}:search")
        self.tech_attempts: Counter[str] = Counter()
        self.behaviour = behaviour
        self.search_calls = 0
        self.download_calls = 0
        self.tech_info_calls = 0
        self.tech_info_failures = 0
        self.states: Counter[str] = Counter()

    def state(self) -> str:
        if self.behaviour == "down":
            return "down"
        if self.behaviour == "slow":
            return "slow"
        if self.behaviour == "flap":
            period = self.args.flap_down + self.args.flap_up
            return "down" if self.clock.elapsed_unscaled() % period < self.args.flap_down else "up"
        return "up"

    async def _transport(self) -> None:
        self.search_calls += 1
        state = self.state()
        self.states[state] += 1
        if state == "down":
            if self.args.failure_mode == "timeout":
                await asyncio.sleep(3600)
            # Like BaseIndexerDefinition: the transport error is wrapped "from None".
            try:
                raise httpx.ConnectError("synthetic connection refused")
            except httpx.ConnectError:
                raise IndexerDefinitionException(f"{self.name} nem érhető el vagy megváltozott a struktúrája.") from None
        latency = self.args.slow_latency if state == "slow" else self.args.latency
        latency *= 1.0 + self.rng.uniform(-self.args.jitter, self.args.jitter)
        await asyncio.sleep(self.clock.scaled(max(0.0, latency)))

    def _torrents(self, key: str, imdb_id: str | None) -> list[IndexerDefinitionTorrent]:
        return [
            IndexerDefinitionTorrent(
                torrent_id=f"{key}-{index:02d}",
                download_url=f"https://{self.id}.fixture.invalid/download/{key}-{index:02d}",
                imdb_id=imdb_id,
                seeders=10 + (index * 7 + len(self.id)) % 40,
                attribute_ids=[],
            )
            for index in range(self.args.results_per_tracker)
        ]

    async def find_torrents_by_imdb_id(self, imdb_id: str) -> list[IndexerDefinitionTorrent]:
        await self._transport()
        return self._torrents(f"{self.id}-{imdb_id}", imdb_id)

    async def find_torrents_by_query(self, query: str, media_type: str | None = None) -> list[IndexerDefinitionTorrent]:
        await self._transport()
        digest = hashlib.sha256(f"{query}:{media_type}".encode()).hexdigest()[:10]
        return self._torrents(f"{self.id}-q{digest}", None)

    async def find_torrent_by_id(self, torrent_id: str) -> IndexerDefinitionTorrent | None:
        await self._transport()
        return self._torrents(torrent_id, None)[0]

    async def download_torrent(self, download_url: str) -> bytes:
        self.download_calls += 1
        await asyncio.sleep(self.clock.scaled(self.args.download_latency))
        torrent_id = download_url.rsplit("/", 1)[-1]
        return build(f"SP.Search.{torrent_id}.2024.1080p.mkv", PIECE_SIZE, TORRENT_SIZE).torrent_bytes

    async def fetch_tech_info(self, torrent_id: str) -> tuple[str | None, str | None]:
        return None, None


class NcoreLikeTracker(SyntheticTracker):
    """nCore-like Tech Info: configurable latency and failure rate (GSP06)."""

    provides_tech_info = True

    async def fetch_tech_info(self, torrent_id: str) -> tuple[str | None, str | None]:
        self.tech_info_calls += 1
        # Failure is a function of (seed, torrent, attempt): the n-th lookup of a torrent fails in
        # both policies alike, whichever of them asks first or prefetches it.
        self.tech_attempts[torrent_id] += 1
        draw = random.Random(f"{self.seed_key}:{torrent_id}:tech:{self.tech_attempts[torrent_id]}").random()
        await asyncio.sleep(self.clock.scaled(self.args.tech_info_latency))
        if draw < self.args.tech_info_failure_rate:
            self.tech_info_failures += 1
            raise TechInfoUnavailableException("nCore Tech Info nem érhető el.")
        return NFO_TEXT, None


def classify_errors(descriptions: list[str]) -> dict[str, Any]:
    kinds = Counter()
    for description in descriptions:
        if description == DEADLINE_ERROR:
            kinds["deadline"] += 1
        elif "átmenetileg kihagyva" in description:
            kinds["skipped"] += 1
        elif "nem válaszolt időben" in description:
            kinds["timeout"] += 1
        else:
            kinds["other"] += 1
    return dict(kinds)


async def drain_background_tasks() -> None:
    drain = getattr(async_utils, "drain_background_tasks", None)
    if drain is not None:
        await drain()


def apply_policy(policy: str, args: argparse.Namespace) -> dict[str, Any]:
    has_breaker = hasattr(config, "indexer_breaker_threshold") and hasattr(search, "IndexerHealth")
    has_prefetch = hasattr(config, "tech_info_prefetch") and tech_info_cache is not None
    if policy == "candidate" and not has_breaker:
        raise RuntimeError("Candidate policy needs SP05/b (IndexerHealth) in the image under test")
    if policy == "prefetch" and not has_prefetch:
        raise RuntimeError("Prefetch policy needs SP06/b (tech_info_prefetch) in the image under test")
    # "prefetch" = SP06/b Tech Info prefetch on, breaker off; the others keep prefetch off.
    prefetch_info: dict[str, Any] = {"tech_info_prefetch": "N/A"}
    if has_prefetch:
        config.tech_info_prefetch = policy == "prefetch"
        prefetch_info = {"tech_info_prefetch": config.tech_info_prefetch,
                         "tech_info_prefetch_top_n": config.tech_info_prefetch_top_n}
    if not hasattr(search, "INDEXER_SEARCH_TIMEOUT_SECONDS") and args.time_scale != 1.0:
        raise RuntimeError("Time scaling needs the SP05/a search timeout constant")
    if hasattr(search, "INDEXER_SEARCH_TIMEOUT_SECONDS"):
        search.INDEXER_SEARCH_TIMEOUT_SECONDS = PRODUCT_SEARCH_TIMEOUT * args.time_scale
    config.source_search_deadline_seconds = PRODUCT_DEADLINE * args.time_scale
    if args.imdb_cache_ttl is not None:
        search._IMDB_TRACKER_CACHE_TTL = args.imdb_cache_ttl * args.time_scale
    if not has_breaker:
        return {"name": policy, "breaker": "N/A", **prefetch_info}
    threshold = args.breaker_threshold if policy == "candidate" else 0
    config.indexer_breaker_threshold = threshold
    config.indexer_breaker_cooldown_seconds = args.breaker_cooldown * args.time_scale
    config.indexer_breaker_cooldown_max_seconds = args.breaker_cooldown_max * args.time_scale
    return {"name": policy, "indexer_breaker_threshold": threshold,
            "indexer_breaker_cooldown_seconds": args.breaker_cooldown,
            "indexer_breaker_cooldown_max_seconds": args.breaker_cooldown_max, **prefetch_info}


async def settle_tech_info_prefetch() -> None:
    """Let SP06/b background Tech Info fetches finish so nothing leaks into the next scenario."""
    prefetcher = getattr(tech_info_cache, "_prefetcher", None)
    if prefetcher is None:
        return
    flights = prefetcher().flights
    while flights:
        await asyncio.gather(*(flight.task for flight in tuple(flights.values())), return_exceptions=True)
        await asyncio.sleep(0)


def clear_tech_info_cache() -> None:
    for module in (tech_info_cache, torrent_streams_module):
        cache = getattr(module, "_TECH_INFO_CACHE", None)
        if cache is not None:
            cache.clear()


async def run_scenario(policy: str, scenario: str, args: argparse.Namespace, run_index: int) -> dict[str, Any]:
    seed_key = f"{args.seed}:{scenario}"
    policy_info = apply_policy(policy, args)
    original_session_local = database.SessionLocal
    with tempfile.TemporaryDirectory(prefix="sp-search-") as directory:
        engine = create_engine(f"sqlite:///{Path(directory) / 'search.db'}",
                               connect_args={"check_same_thread": False, "timeout": 10.0}, hide_parameters=True)
        database.Base.metadata.create_all(engine)
        database.SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=True)
        api_key = f"sp-search-{secrets.token_hex(16)}"
        clock = ScenarioClock(args.time_scale)
        trackers: dict[str, SyntheticTracker] = {}
        subject_id = ""
        try:
            with Session(engine) as db:
                db.add(RoleModel(id="user", name="Synthetic"))
                db.flush()
                user = UserModel(id="sp-search-user", username="sp-search-user", api_key=api_key)
                db.add(user)
                for index in range(args.trackers):
                    indexer_id = f"sp-{chr(ord('a') + index)}"
                    name = f"Synthetic {chr(ord('A') + index)}"
                    subject = index == args.trackers - 1
                    behaviour = "healthy"
                    if subject:
                        subject_id = indexer_id
                        behaviour = {"S1": "healthy", "S2": "down", "S3": "slow", "S4": "flap"}[scenario]
                    tracker_type = NcoreLikeTracker if index == 0 else SyntheticTracker
                    trackers[indexer_id] = tracker_type(indexer_id, name, args, clock, seed_key, behaviour)
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
                health = CountingHealth(clock)
                search.indexer_health = health
            streams.invalidate_stream_cache()
            clear_tech_info_cache()
            info_cache = getattr(torrent_models, "_TORRENT_INFO_CACHE", None)
            if info_cache is not None:
                info_cache.clear()

            settings = SimpleNamespace(get_app_url=lambda: BASE_URL)
            inventory = SimpleNamespace(is_file_downloaded=lambda **kwargs: False, get_torrents=lambda: [])
            resolver = SimpleNamespace(get_alternate_imdb_ids=_no_aliases, get_title_by_imdb=_no_title)

            def stremio_service(db: Annotated[Session, Depends(get_db, scope="function")]):
                indexers = IndexerSearchService(registry)  # type: ignore[arg-type]
                provider = provider_module.TorrentSourceProviderService(
                    indexers, IsolatedTorrentFilesService(), **provider_extra)
                torrent_streams = torrent_streams_module.TorrentStreamsService(
                    db, provider, inventory, settings, create_preferences_service(db),  # type: ignore[arg-type]
                    indexer_definitions_service=registry,  # type: ignore[arg-type]
                )
                return streams.StremioService(torrent_streams, settings, tvdb_resolver_service=resolver)  # type: ignore[arg-type]

            # SP06/b: the shared search gets the (process-wide) definitions registry for prefetch,
            # exactly like the product DI; older images have no such parameter.
            provider_extra: dict[str, Any] = (
                {"indexer_definitions_service": registry}
                if "indexer_definitions_service" in inspect.signature(provider_module.TorrentSourceProviderService).parameters
                else {})
            app.dependency_overrides[get_stremio_service] = stremio_service
            rows: list[dict[str, Any]] = []
            cpu_before = time.process_time()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as client:

                async def request(kind: str, index: int, imdb_id: str) -> None:
                    subject_state = trackers[subject_id].state()
                    offset = clock.elapsed_unscaled()
                    began = time.perf_counter()
                    response = await client.get(f"/api/{api_key}/stremio/stream/movie/{imdb_id}.json")
                    latency_ms = (time.perf_counter() - began) * 1000 / args.time_scale
                    if response.status_code != 200:
                        raise RuntimeError(f"Synthetic stream request failed with status {response.status_code}")
                    body = response.json()["streams"]
                    playable = [stream for stream in body if stream.get("url")]
                    errors = [stream.get("description", "") for stream in body if stream.get("name") == ERROR_STREAM_NAME]
                    files = sorted((stream.get("behaviorHints") or {}).get("filename") or "" for stream in playable)
                    # GSP06: the fixture NFO carries a Hungarian subtitle track, the torrent names do not,
                    # so the subtitle marker in a description means Tech Info reached that stream.
                    enriched = [stream for stream in playable if TECH_INFO_MARKER in (stream.get("description") or "")]
                    rendered = sorted(f"{(stream.get('behaviorHints') or {}).get('filename') or ''}|"
                                      f"{stream.get('description') or ''}" for stream in playable)
                    by_tracker = Counter()
                    for file_name in files:
                        for indexer_id in trackers:
                            if f".{indexer_id}-" in file_name:
                                by_tracker[indexer_id] += 1
                    error_kinds = classify_errors(errors)
                    rows.append({
                        "kind": kind, "index": index, "offset_s": round(offset, 3), "latency_ms": round(latency_ms, 3),
                        "subject_state": subject_state, "sources": len(playable), "sources_by_tracker": dict(by_tracker),
                        "source_digest": hashlib.sha256("\n".join(files).encode()).hexdigest(),
                        "tech_enriched": len(enriched),
                        "enrichment_digest": hashlib.sha256("\n".join(rendered).encode()).hexdigest(),
                        "tech_info_calls_total": sum(t.tech_info_calls for t in trackers.values()),
                        "errors": error_kinds, "partial": bool(error_kinds.get("deadline")),
                    })

                for index in range(args.requests):
                    due = clock.scaled(index * args.interval) - (time.perf_counter() - clock.started)
                    if due > 0:
                        await asyncio.sleep(due)
                    imdb_id = f"tt{9_000_000 + run_index * 100_000 + index:07d}"
                    await request("cold", index, imdb_id)
                    await drain_background_tasks()
                    streams.invalidate_stream_cache(user_id)
                    await request("warm", index, imdb_id)
                    await drain_background_tasks()
            elapsed_unscaled = clock.elapsed_unscaled()
            await settle_tech_info_prefetch()
            cpu_seconds = time.process_time() - cpu_before
            if engine.pool.checkedout():
                raise RuntimeError("Synthetic search fixture left a checked-out connection")
        finally:
            app.dependency_overrides.pop(get_stremio_service, None)
            invalidate_indexer_cache()
            streams.invalidate_stream_cache()
            database.SessionLocal = original_session_local
            engine.dispose()

    subject = trackers[subject_id]
    summary: dict[str, Any] = {"scenario": scenario, "description": SCENARIOS[scenario], "policy": policy_info,
                               "subject_tracker": subject_id, "elapsed_unscaled_s": round(elapsed_unscaled, 3),
                               "cpu_seconds": round(cpu_seconds, 3)}
    for kind in ("cold", "warm"):
        selected = [row for row in rows if row["kind"] == kind]
        steady = [row for row in selected if row["index"] >= args.steady_from]
        summary[kind] = {
            "latency_ms": distribution([row["latency_ms"] for row in selected]),
            "steady_latency_ms": distribution([row["latency_ms"] for row in steady]),
            "steady_sources": distribution([float(row["sources"]) for row in steady]),
            "steady_partial_rate": sum(row["partial"] for row in steady) / len(steady),
            "steady_tech_enriched": distribution([float(row["tech_enriched"]) for row in steady]),
            "sources": distribution([float(row["sources"]) for row in selected]),
            "healthy_sources": distribution([float(sum(n for t, n in row["sources_by_tracker"].items() if t != subject_id))
                                             for row in selected]),
            "partial_rate": sum(row["partial"] for row in selected) / len(selected),
            "skipped_rate": sum(bool(row["errors"].get("skipped")) for row in selected) / len(selected),
            "timeout_error_rate": sum(bool(row["errors"].get("timeout")) for row in selected) / len(selected),
        }
    minutes = elapsed_unscaled / 60
    summary["subject_requests"] = {
        "search_calls": subject.search_calls,
        "search_calls_per_minute": round(subject.search_calls / minutes, 3) if minutes > 0 else None,
        "states_seen": dict(subject.states),
        "download_calls": subject.download_calls,
    }
    summary["breaker"] = ({"openings": health.openings[subject_id], "probes": health.probes[subject_id],
                           "opened_at_s": health.opened_at.get(subject_id, []),
                           "openings_other_trackers": sum(n for t, n in health.openings.items() if t != subject_id)}
                          if health is not None else "N/A")
    tech_calls = sum(t.tech_info_calls for t in trackers.values())
    summary["tech_info"] = {"calls": tech_calls, "failures": sum(t.tech_info_failures for t in trackers.values()),
                            "calls_per_cold_request": round(tech_calls / args.requests, 3)}
    summary["requests"] = rows
    return summary


async def _no_aliases(*args: Any, **kwargs: Any) -> list[str]:
    return []


async def _no_title(*args: Any, **kwargs: Any) -> None:
    return None


async def main(args: argparse.Namespace) -> dict[str, Any]:
    attempted_external = 0
    original_send = httpx.AsyncClient.send

    async def offline_send(client, request, *send_args, **send_kwargs):
        nonlocal attempted_external
        if request.url.host != "fixture.invalid":
            attempted_external += 1
            raise RuntimeError("Offline search harness blocked external HTTP")
        return await original_send(client, request, *send_args, **send_kwargs)

    imdb_cache_ttl = getattr(search, "_IMDB_TRACKER_CACHE_TTL", None)
    defaults = {name: getattr(config, name) for name in (
        "source_search_deadline_seconds", "indexer_breaker_threshold", "tech_info_prefetch",
        "indexer_breaker_cooldown_seconds", "indexer_breaker_cooldown_max_seconds") if hasattr(config, name)}
    httpx.AsyncClient.send = offline_send  # type: ignore[method-assign]
    runs = []
    try:
        run_index = 0
        for policy in args.policies:
            for scenario in args.scenarios:
                runs.append(await run_scenario(policy, scenario, args, run_index))
                run_index += 1
    finally:
        httpx.AsyncClient.send = original_send  # type: ignore[method-assign]
        for name, value in defaults.items():
            setattr(config, name, value)
        if hasattr(search, "INDEXER_SEARCH_TIMEOUT_SECONDS"):
            search.INDEXER_SEARCH_TIMEOUT_SECONDS = PRODUCT_SEARCH_TIMEOUT
        if imdb_cache_ttl is not None:
            search._IMDB_TRACKER_CACHE_TTL = imdb_cache_ttl
    script = Path(__file__).resolve()
    return {
        "fixture": f"{FIXTURE}-smoke" if args.smoke else FIXTURE,
        "is_measurement": not args.smoke,
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "python": platform.python_version(),
        "parameters": {key: value for key, value in vars(args).items() if key != "out"},
        "product_defaults": {"indexer_search_timeout_seconds": PRODUCT_SEARCH_TIMEOUT,
                             "source_search_deadline_seconds": PRODUCT_DEADLINE},
        "runs": runs,
        "external_http_attempts": attempted_external,
        "scope": ("Real API-key/DB auth, Stremio/provider/repository path, IndexerSearchService (timeout, tracker "
                  "cache, SP05/b breaker), shared source search (singleflight, deadline, .torrent download/store), "
                  "Tech Info enrichment, filter/sort/serialization. Synthetic tracker adapters and nCore-like Tech "
                  "Info stub; no real tracker, account or network. Sequential requests; cold = fresh IMDb id, warm = "
                  "same id with the user's stream response cache invalidated."),
    }


if __name__ == "__main__":
    arguments = parse_args()
    result = asyncio.run(main(arguments))
    if result["external_http_attempts"]:
        raise SystemExit("External HTTP attempted; result discarded")
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if arguments.out:
        Path(arguments.out).write_text(text + "\n", encoding="utf-8")
        print(json.dumps({"fixture": result["fixture"], "runs": len(result["runs"]), "out": arguments.out}))
    else:
        print(text)

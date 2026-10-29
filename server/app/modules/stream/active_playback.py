import asyncio
import time

# (api_key) -> list of (binge_group, timestamp)
_ACTIVE_PLAYBACKS: dict[str, list[tuple[str, float]]] = {}
_PLAYBACK_EVENTS: dict[str, asyncio.Event] = {}
_PLAYBACK_TTL_SECONDS = 7200.0  # 2 óra


def record_active_playback(
    api_key: str,
    indexer_id: str,
    torrent_id: str,
) -> None:
    """Rögzíti a felhasználó által éppen elindított streamet (indexer_id-torrent_id)."""
    now = time.monotonic()
    binge_group = f"{indexer_id}-{torrent_id}"

    history = _ACTIVE_PLAYBACKS.get(api_key, [])
    history = [
        (bg, ts)
        for bg, ts in history
        if (now - ts) < _PLAYBACK_TTL_SECONDS and bg != binge_group
    ]
    history.insert(0, (binge_group, now))
    _ACTIVE_PLAYBACKS[api_key] = history[:20]

    event = _PLAYBACK_EVENTS.get(api_key)
    if event is not None:
        event.set()


def find_active_binge_group(
    api_key: str,
    valid_binge_groups: set[str],
    max_age_seconds: float = _PLAYBACK_TTL_SECONDS,
) -> str | None:
    """Megkeresi a legutóbbi aktív lejátszást, amely szerepel az adott epizód/film streamjei között."""
    if not valid_binge_groups:
        return None
    now = time.monotonic()
    for bg, ts in _ACTIVE_PLAYBACKS.get(api_key, []):
        if (now - ts) <= max_age_seconds and bg in valid_binge_groups:
            return bg
    return None


async def wait_for_active_binge_group(
    api_key: str,
    valid_binge_groups: set[str],
    timeout: float = 2.0,
) -> str | None:
    """
    Ha a lejátszó a /subtitles végpontot pár száz ezredmásodperccel a /stream/{token}
    indulása előtt hívja meg, megvárja a stream elindulását (legfeljebb `timeout` másodpercig).
    """
    if not valid_binge_groups:
        return None

    # 1. Ha az elmúlt néhány másodpercben már elindult az egyik stream, azonnal visszaadjuk
    recent = find_active_binge_group(api_key, valid_binge_groups, max_age_seconds=30.0)
    if recent:
        return recent

    # 2. Várunk röviden, hátha a lejátszó épp most nyitja meg a /stream/{token} URL-t
    event = asyncio.Event()
    _PLAYBACK_EVENTS[api_key] = event
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    finally:
        if _PLAYBACK_EVENTS.get(api_key) is event:
            _PLAYBACK_EVENTS.pop(api_key, None)

    return find_active_binge_group(api_key, valid_binge_groups)


def clear_active_playbacks() -> None:
    """Teszteléshez törli a memóriabeli aktív lejátszásokat."""
    _ACTIVE_PLAYBACKS.clear()
    _PLAYBACK_EVENTS.clear()


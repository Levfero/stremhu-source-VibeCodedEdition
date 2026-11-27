"""A VPN pillanatnyi állapota a streamelés számára (VPN-terv Rev. 5, R10).

A kezelő induláskor regisztrálja magát. Kezelő nélkül (pl. tesztben) a
torrentforgalom elérhetőnek számít.
"""

from collections.abc import Callable

_provider: Callable[[], bool] | None = None


def register(provider: Callable[[], bool]) -> None:
    global _provider
    _provider = provider


def unregister(provider: Callable[[], bool]) -> None:
    global _provider
    # Kötött metódusnál minden hozzáférés új objektum, ezért egyenlőséggel hasonlítunk.
    if _provider == provider:
        _provider = None


def torrent_traffic_available() -> bool:
    """Hamis, ha a VPN be van kapcsolva, de a tunnel nem él: új letöltés ilyenkor nem indulhat."""
    provider = _provider
    return True if provider is None else provider()

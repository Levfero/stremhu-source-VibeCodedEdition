"""A libtorrent session hálózati szabálya (VPN-terv Rev. 5, L1, L3, L6).

A session zárt hálózati beállításokkal indul: nincs UPnP/NAT-PMP/LSD/DHT, és a
listener csak loopbacken van. Torrent csak egy szabály (közvetlen vagy VPN)
sikeres alkalmazása után kerülhet a sessionbe.
"""

from dataclasses import dataclass, field

import libtorrent as libtorrent

LOOPBACK_LISTEN = "127.0.0.1:0"
PROXY_HOST = "127.0.0.1"


@dataclass(frozen=True)
class DirectPolicy:
    """Közvetlen mód: a felhasználó relay-hálózati beállításai érvényesek."""


@dataclass(frozen=True)
class VpnProxyPolicy:
    """VPN mód: minden torrentforgalom a helyi, hitelesített SOCKS5 proxyn megy."""

    port: int
    username: str = field(repr=False)
    password: str = field(repr=False)


NetworkPolicy = DirectPolicy | VpnProxyPolicy


@dataclass(frozen=True)
class UserNetworkSettings:
    """A felhasználó hálózati relay-beállításai; csak közvetlen módban érvényesek."""

    port: int
    enable_upnp_and_natpmp: bool = False
    enable_utp: bool = True


def closed_network_settings() -> "libtorrent.settings_pack":
    """Indulócsomag: a szabály alkalmazásáig semmi nem figyel és nem nyit portot a valódi hálózaton."""
    return {
        "listen_interfaces": LOOPBACK_LISTEN,
        "enable_upnp": False,
        "enable_natpmp": False,
        "enable_lsd": False,
        "enable_dht": False,
        "dht_bootstrap_nodes": "",
    }


def direct_settings(user: UserNetworkSettings) -> "libtorrent.settings_pack":
    """Közvetlen mód: proxy nélkül, a felhasználó portjával, UPnP/NAT-PMP és uTP beállításával."""
    return {
        "proxy_type": int(libtorrent.proxy_type_t.none),
        "proxy_hostname": "",
        "proxy_port": 0,
        "proxy_username": "",
        "proxy_password": "",
        "outgoing_interfaces": "",
        "listen_interfaces": f"0.0.0.0:{user.port},[::]:{user.port}",
        "enable_upnp": user.enable_upnp_and_natpmp,
        "enable_natpmp": user.enable_upnp_and_natpmp,
        "enable_outgoing_utp": user.enable_utp,
        "enable_incoming_utp": user.enable_utp,
    }


def vpn_settings(policy: VpnProxyPolicy) -> "libtorrent.settings_pack":
    """VPN mód: csak proxyn át, loopback-listenerrel; DHT, LSD, UPnP, NAT-PMP és uTP kikapcsolva."""
    return {
        **closed_network_settings(),
        "proxy_type": int(libtorrent.proxy_type_t.socks5_pw),
        "proxy_hostname": PROXY_HOST,
        "proxy_port": policy.port,
        "proxy_username": policy.username,
        "proxy_password": policy.password,
        "proxy_hostnames": True,
        "proxy_peer_connections": True,
        "proxy_tracker_connections": True,
        # Mélységi védelem (L3, G2-ben mérve: a letöltés megy, 0 közvetlen csomag).
        # A libtorrent SOCKS5-UDP socketje ettől is 0.0.0.0-ra köt; azon nem megy ki semmi.
        "outgoing_interfaces": "127.0.0.1",
        "enable_outgoing_utp": False,
        "enable_incoming_utp": False,
        # A privát trackerek kliens-fehérlistája miatt az anonim mód nem kapcsolható be.
        "anonymous_mode": False,
    }

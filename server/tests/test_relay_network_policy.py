import threading
from typing import cast
from unittest.mock import Mock

import libtorrent
import pytest
from fastapi import HTTPException

from app.modules.relay.network_policy import (
    DirectPolicy,
    UserNetworkSettings,
    VpnProxyPolicy,
)
from app.modules.relay.schemas import RelaySettingsUpdate
from app.modules.relay.service import RelayService
from app.modules.settings.enums import RelayEncryptionEnum

pytestmark = pytest.mark.anyio

VPN = VpnProxyPolicy(port=41234, username="fixture-user", password="fixture-pass")


async def _relay() -> RelayService:
    return RelayService()


def _settings(relay: RelayService) -> dict[str, object]:
    return dict(relay._libtorrent_session.get_settings())


async def test_session_starts_closed_and_refuses_torrents_until_a_policy():
    relay = await _relay()
    try:
        settings = _settings(relay)
        assert settings["listen_interfaces"] == "127.0.0.1:0"
        assert not settings["enable_upnp"] and not settings["enable_natpmp"]
        assert not settings["enable_lsd"] and not settings["enable_dht"]
        assert settings["dht_bootstrap_nodes"] == ""
        with pytest.raises(HTTPException) as raised:
            relay.add_torrent(b"not-used")
        assert raised.value.status_code == 503
    finally:
        await relay.close_native()


def test_user_network_settings_wait_for_the_direct_policy():
    # Mock session: a valódi interfészekre kötés (0.0.0.0) nem kell a teszthez.
    relay = _mock_relay(None)
    session = cast(Mock, relay._libtorrent_session)
    relay.update_settings(RelaySettingsUpdate(port=6999, enable_utp=False, enable_upnp_and_natpmp=True))
    applied_before = session.apply_settings.call_args.args[0]
    assert not {"listen_interfaces", "enable_upnp", "enable_natpmp", "enable_outgoing_utp"} & set(applied_before)

    relay.apply_network_policy(DirectPolicy())
    pack = session.apply_settings.call_args.args[0]
    assert pack["listen_interfaces"] == "0.0.0.0:6999,[::]:6999"
    assert pack["enable_upnp"] and pack["enable_natpmp"]
    assert not pack["enable_outgoing_utp"] and not pack["enable_incoming_utp"]
    assert pack["proxy_type"] == int(libtorrent.proxy_type_t.none)


async def test_vpn_policy_forces_proxy_and_gui_cannot_reopen_the_real_network():
    relay = await _relay()
    try:
        relay.apply_network_policy(VPN)
        relay.update_settings(
            RelaySettingsUpdate(
                port=6999,
                enable_upnp_and_natpmp=True,
                enable_utp=True,
                encryption=RelayEncryptionEnum.FORCED,
            )
        )
        settings = _settings(relay)
        assert settings["proxy_type"] == int(libtorrent.proxy_type_t.socks5_pw)
        assert settings["proxy_hostname"] == "127.0.0.1" and settings["proxy_port"] == VPN.port
        assert settings["proxy_username"] == VPN.username
        assert settings["proxy_hostnames"] and settings["proxy_peer_connections"]
        assert settings["proxy_tracker_connections"]
        assert settings["listen_interfaces"] == "127.0.0.1:0"
        assert not settings["enable_upnp"] and not settings["enable_natpmp"]
        assert not settings["enable_outgoing_utp"] and not settings["enable_incoming_utp"]
        assert not settings["enable_dht"] and not settings["anonymous_mode"]
        # A titkosítás VPN módban is a felhasználó beállítása marad.
        assert settings["out_enc_policy"] == int(libtorrent.enc_policy.forced)
    finally:
        await relay.close_native()


def _mock_relay(policy):
    relay = object.__new__(RelayService)
    relay._network_lock = threading.RLock()
    relay._network_policy = policy
    relay._user_network = UserNetworkSettings(port=6881)
    relay._libtorrent_session = Mock()
    relay._accepting = True
    return relay


def test_runtime_switch_pauses_applies_then_resumes():
    relay = _mock_relay(DirectPolicy())
    relay.apply_network_policy(VPN)
    names = [call[0] for call in cast(Mock, relay._libtorrent_session).method_calls]
    assert names == ["pause", "apply_settings", "resume"]
    assert relay.network_policy == VPN


def test_failed_switch_stays_paused_without_policy_and_refuses_torrents():
    relay = _mock_relay(VPN)
    cast(Mock, relay._libtorrent_session).apply_settings.side_effect = RuntimeError("fixture")
    with pytest.raises(RuntimeError):
        relay.apply_network_policy(DirectPolicy())
    names = [call[0] for call in cast(Mock, relay._libtorrent_session).method_calls]
    assert names == ["pause", "apply_settings"]
    assert relay.network_policy is None
    with pytest.raises(HTTPException) as raised:
        relay.add_torrent(b"not-used")
    assert raised.value.status_code == 503

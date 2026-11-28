import asyncio
from functools import lru_cache

from app.common.database import db_session
from app.config import config
from app.modules.relay.dependencies import get_relay_service
from app.modules.relay.service import RelayService
from app.modules.settings.dependencies import create_settings_service
from app.modules.settings.schemas.internal import VpnSettings, VpnSettingsUpdate
from app.modules.vpn.manager import VpnManager
from app.modules.vpn.proxy_supervisor import ProxySupervisor
from app.modules.vpn.secret_store import KEY_FILENAME, SecretStore
from app.modules.vpn.service import VpnService
from app.modules.vpn.speedtest import VpnSpeedTest


def _load_vpn_settings() -> VpnSettings:
    with db_session() as db:
        return create_settings_service(db).get_vpn()


def _save_vpn_settings(update: VpnSettingsUpdate) -> VpnSettings:
    with db_session() as db:
        return create_settings_service(db).save_vpn(update)


def create_secret_store() -> SecretStore:
    return SecretStore.from_settings(config.vpn_secret_key, config.system_dir / KEY_FILENAME)


@lru_cache(maxsize=1)
def get_vpn_manager(relay: RelayService) -> VpnManager:
    """A lifespan saját relayjét kapja: a kezelő soha nem hoz létre második sessiont."""
    return VpnManager(
        relay,
        load_settings=_load_vpn_settings,
        save_settings=_save_vpn_settings,
        secret_store_factory=create_secret_store,
        supervisor=ProxySupervisor(config.vpn_wireproxy_path, config.vpn_runtime_dir),
        check_alive=config.vpn_check_alive,
        check_alive_interval=config.vpn_check_alive_interval,
        failover_after=config.vpn_failover_after,
    )


def create_speed_test() -> VpnSpeedTest:
    return VpnSpeedTest(
        download_url=config.vpn_speedtest_download_url,
        trace_url=config.vpn_speedtest_trace_url,
    )


@lru_cache(maxsize=1)
def get_speed_test_lock() -> asyncio.Lock:
    """Egyszerre egy sebességteszt futhat (a szolgáltatás kérésenként új példány)."""
    return asyncio.Lock()


def get_vpn_service() -> VpnService:
    relay = get_relay_service()
    return VpnService(
        get_vpn_manager(relay),
        load_settings=_load_vpn_settings,
        save_settings=_save_vpn_settings,
        secret_store_factory=create_secret_store,
        network_settings=relay.network_settings,
        speed_test_factory=create_speed_test,
        speed_test_lock=get_speed_test_lock(),
    )

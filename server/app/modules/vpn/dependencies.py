from functools import lru_cache

from app.common.database import db_session
from app.config import config
from app.modules.relay.service import RelayService
from app.modules.settings.dependencies import create_settings_service
from app.modules.settings.schemas.internal import VpnSettings, VpnSettingsUpdate
from app.modules.vpn.manager import VpnManager
from app.modules.vpn.proxy_supervisor import ProxySupervisor
from app.modules.vpn.secret_store import KEY_FILENAME, SecretStore


def _load_vpn_settings() -> VpnSettings:
    with db_session() as db:
        return create_settings_service(db).get_vpn()


def _save_vpn_settings(update: VpnSettingsUpdate) -> None:
    with db_session() as db:
        create_settings_service(db).save_vpn(update)


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
    )

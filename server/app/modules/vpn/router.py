from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.config import show_internal_routes
from app.modules.auth.dependencies import SessionGuard
from app.modules.roles.constants import UserRoleKey
from app.modules.users.models import UserModel
from app.modules.vpn.dependencies import get_vpn_service
from app.modules.vpn.schemas import (
    VpnResponse,
    VpnSpeedTestResponse,
    VpnStatusResponse,
    VpnTestResponse,
    VpnUpdateRequest,
)
from app.modules.vpn.service import VpnService

router = APIRouter(tags=["VPN"])

# Az admin-ellenőrzés áll elöl: hitelesítés nélküli kérés nem hoz létre szolgáltatást.
AdminUser = Annotated[UserModel, Depends(SessionGuard([UserRoleKey.ADMIN]))]
Service = Annotated[VpnService, Depends(get_vpn_service)]


@router.get("/vpn", response_model=VpnResponse, include_in_schema=show_internal_routes())
async def get_vpn(_: AdminUser, service: Service):
    return await service.get()


@router.put("/vpn", response_model=VpnResponse, include_in_schema=show_internal_routes())
async def update_vpn(_: AdminUser, payload: VpnUpdateRequest, service: Service):
    return await service.update(payload)


ConfigId = Annotated[str, Path(max_length=32, pattern=r"^[A-Za-z0-9_-]+$")]


@router.delete(
    "/vpn/configs/{config_id}", response_model=VpnResponse, include_in_schema=show_internal_routes()
)
async def delete_vpn_config(_: AdminUser, config_id: ConfigId, service: Service):
    return await service.delete_config(config_id)


@router.get("/vpn/status", response_model=VpnStatusResponse, include_in_schema=show_internal_routes())
def get_vpn_status(_: AdminUser, service: Service):
    return service.status()


@router.post("/vpn/test", response_model=VpnTestResponse, include_in_schema=show_internal_routes())
async def test_vpn(_: AdminUser, service: Service):
    return await service.test()


@router.post("/vpn/speedtest", response_model=VpnSpeedTestResponse, include_in_schema=show_internal_routes())
async def speed_test_vpn(_: AdminUser, service: Service):
    return await service.speed_test()

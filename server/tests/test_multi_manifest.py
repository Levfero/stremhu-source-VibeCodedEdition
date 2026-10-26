from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.auth.dependencies import get_auth_service
from app.modules.auth.service import AuthService
from app.modules.roles.constants import UserRoleKey
from app.modules.settings.dependencies import get_settings_service
from app.modules.settings.schemas.internal import ManifestEndpointInfo, ManifestSettings
from app.modules.settings.service import SettingsService
from app.modules.stremio.dependencies import get_stremio_service
from app.modules.stremio.schemas import BehaviorHints, Manifest, StremioStream
from app.modules.stremio.service import StremioService
from app.modules.users.models import UserModel


@pytest.fixture
def client():
    return TestClient(app)


def test_multi_manifest_endpoints(client):
    mock_stremio_service = AsyncMock(spec=StremioService)
    base_manifest = Manifest(
        id="stremhu.source",
        name="StremHU Source",
        description="StremHU Source Addon",
        version="1.0.0",
        resources=["stream"],
        types=["movie", "series"],
        catalogs=[],
    )
    mock_stremio_service.manifest.return_value = base_manifest

    mock_auth_service = AsyncMock(spec=AuthService)
    mock_auth_service.verify_api_key.return_value = UserModel(
        id="user123", username="testuser"
    )

    mock_settings_service = MagicMock(spec=SettingsService)
    mock_settings_service.get_app_url.return_value = "https://stremio-ncore-addon.levfero.stream"
    mock_settings_service.get_manifest_url.side_effect = lambda ep: {
        "lan": "http://192.168.1.5:7070",
        "tailscale": "http://100.66.1.85:7070",
    }.get(ep)

    app.dependency_overrides[get_stremio_service] = lambda: mock_stremio_service
    app.dependency_overrides[get_auth_service] = lambda: mock_auth_service
    app.dependency_overrides[get_settings_service] = lambda: mock_settings_service

    try:
        # 1. Default manifest
        res_default = client.get("/api/dummy_key/stremio/manifest.json")
        assert res_default.status_code == 200
        m_default = res_default.json()
        assert m_default["name"] == "StremHU Source"
        assert m_default["id"] == "stremhu.source"

        # 2. LAN manifest
        res_lan = client.get("/api/dummy_key/stremio-lan/manifest.json")
        assert res_lan.status_code == 200
        m_lan = res_lan.json()
        assert "LAN" in m_lan["name"]
        assert m_lan["id"] == "stremhu.source.lan"
        assert "http://192.168.1.5:7070" in m_lan["logo"]

        # 3. Tailscale manifest
        res_ts = client.get("/api/dummy_key/stremio-tailscale/manifest.json")
        assert res_ts.status_code == 200
        m_ts = res_ts.json()
        assert "Tailscale" in m_ts["name"]
        assert m_ts["id"] == "stremhu.source.tailscale"
        assert "http://100.66.1.85:7070" in m_ts["logo"]

    finally:
        app.dependency_overrides.clear()


def test_multi_manifest_stream_url_rewriting(client):
    mock_stremio_service = AsyncMock(spec=StremioService)
    mock_stremio_service.get_streams.return_value = [
        StremioStream(
            name="1080p Stream",
            description="Test stream",
            url="https://stremio-ncore-addon.levfero.stream/api/dummy_key/stream/test_token",
            behavior_hints=BehaviorHints(not_web_ready=True),
        )
    ]

    mock_auth_service = AsyncMock(spec=AuthService)
    mock_auth_service.verify_api_key.return_value = UserModel(
        id="user123", username="testuser"
    )

    mock_settings_service = MagicMock(spec=SettingsService)
    mock_settings_service.get_app_url.return_value = "https://stremio-ncore-addon.levfero.stream"
    mock_settings_service.get_manifest_url.side_effect = lambda ep: {
        "lan": "http://192.168.1.5:7070",
        "tailscale": "http://100.66.1.85:7070",
    }.get(ep)

    app.dependency_overrides[get_stremio_service] = lambda: mock_stremio_service
    app.dependency_overrides[get_auth_service] = lambda: mock_auth_service
    app.dependency_overrides[get_settings_service] = lambda: mock_settings_service

    try:
        # Default endpoint leaves the main URL intact
        res_default = client.get("/api/dummy_key/stremio/stream/movie/tt1234567.json")
        assert res_default.status_code == 200
        stream_def = res_default.json()["streams"][0]
        assert "https://stremio-ncore-addon.levfero.stream" in stream_def["url"]

        # LAN endpoint rewrites stream URL to LAN base URL
        res_lan = client.get("/api/dummy_key/stremio-lan/stream/movie/tt1234567.json")
        assert res_lan.status_code == 200
        stream_lan = res_lan.json()["streams"][0]
        assert stream_lan["url"].startswith("http://192.168.1.5:7070/api/dummy_key/stream/test_token")

        # Tailscale endpoint rewrites stream URL to Tailscale base URL
        res_ts = client.get("/api/dummy_key/stremio-tailscale/stream/movie/tt1234567.json")
        assert res_ts.status_code == 200
        stream_ts = res_ts.json()["streams"][0]
        assert stream_ts["url"].startswith("http://100.66.1.85:7070/api/dummy_key/stream/test_token")

    finally:
        app.dependency_overrides.clear()

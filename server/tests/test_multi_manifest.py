from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.modules.auth.dependencies import get_auth_service
from app.modules.auth.service import AuthService
from app.modules.settings.service import SettingsService
from app.modules.stremio.dependencies import get_stremio_service
from app.modules.stremio.schemas import BehaviorHints, StremioStream
from app.modules.stremio.service import StremioService
from app.modules.torrent_streams.service import TorrentStreamsService
from app.modules.users.models import UserModel


@pytest.fixture
def client():
    return TestClient(app)


def test_multi_manifest_endpoints(client):
    mock_auth_service = AsyncMock(spec=AuthService)
    mock_auth_service.verify_api_key.return_value = UserModel(
        id="user123", username="testuser"
    )

    mock_settings_service = MagicMock(spec=SettingsService)
    mock_settings_service.get_app_url.return_value = "https://stremio.example.com"
    mock_settings_service.get_manifest_url.side_effect = lambda ep: {
        "lan": "http://192.168.1.100:7070",
        "tailscale": "http://100.64.0.1:7070",
    }.get(ep)
    from app.modules.settings.schemas.internal import ManifestEndpointInfo
    mock_settings_service.get_endpoint_by_id.side_effect = lambda ep: {
        "lan": ManifestEndpointInfo(id="lan", name="LAN", badge="LAN", url_suffix="stremio-lan", base_url="http://192.168.1.100:7070", is_enabled=True),
        "tailscale": ManifestEndpointInfo(id="tailscale", name="Tailscale", badge="Tailscale", url_suffix="stremio-tailscale", base_url="http://100.64.0.1:7070", is_enabled=True),
    }.get(ep)

    mock_torrent_streams_service = AsyncMock(spec=TorrentStreamsService)

    real_stremio_service = StremioService(
        torrent_streams_service=mock_torrent_streams_service,
        settings_service=mock_settings_service,
    )

    app.dependency_overrides[get_stremio_service] = lambda: real_stremio_service
    app.dependency_overrides[get_auth_service] = lambda: mock_auth_service

    try:
        # 1. Default manifest
        res_default = client.get("/api/dummy_key/stremio/manifest.json")
        assert res_default.status_code == 200
        m_default = res_default.json()
        assert m_default["name"] == "StremHU Source"
        assert m_default["id"] == "hu.stremhu-source.addon"

        # 2. LAN manifest
        res_lan = client.get("/api/dummy_key/stremio-lan/manifest.json")
        assert res_lan.status_code == 200
        m_lan = res_lan.json()
        assert m_lan["name"] == "StremHU Source (LAN)"
        assert m_lan["id"] == "hu.stremhu-source.addon.lan"
        assert "http://192.168.1.100:7070" in m_lan["logo"]

        # 3. Tailscale manifest
        res_ts = client.get("/api/dummy_key/stremio-tailscale/manifest.json")
        assert res_ts.status_code == 200
        m_ts = res_ts.json()
        assert m_ts["name"] == "StremHU Source (Tailscale)"
        assert m_ts["id"] == "hu.stremhu-source.addon.tailscale"
        assert "http://100.64.0.1:7070" in m_ts["logo"]

    finally:
        app.dependency_overrides.clear()


def test_multi_manifest_stream_url_rewriting(client):
    mock_auth_service = AsyncMock(spec=AuthService)
    mock_auth_service.verify_api_key.return_value = UserModel(
        id="user123", username="testuser"
    )

    mock_settings_service = MagicMock(spec=SettingsService)
    mock_settings_service.get_app_url.return_value = "https://stremio.example.com"
    mock_settings_service.get_manifest_url.side_effect = lambda ep: {
        "lan": "http://192.168.1.100:7070",
        "tailscale": "http://100.64.0.1:7070",
    }.get(ep)

    mock_stremio_service = AsyncMock(spec=StremioService)

    async def fake_get_streams(user, parsed_id, endpoint_type="default"):
        streams = [
            StremioStream(
                name="1080p Stream",
                description="Test stream",
                url="https://stremio.example.com/api/dummy_key/stream/test_token",
                behavior_hints=BehaviorHints(not_web_ready=True),
            )
        ]
        target_url = (
            mock_settings_service.get_manifest_url(endpoint_type)
            if endpoint_type != "default"
            else None
        )
        if target_url:
            main_url = mock_settings_service.get_app_url().rstrip("/")
            for s in streams:
                if s.url and main_url in s.url:
                    s.url = s.url.replace(main_url, target_url.rstrip("/"))
        return streams

    mock_stremio_service.get_streams.side_effect = fake_get_streams

    app.dependency_overrides[get_stremio_service] = lambda: mock_stremio_service
    app.dependency_overrides[get_auth_service] = lambda: mock_auth_service

    try:
        # Default endpoint leaves the main URL intact
        res_default = client.get("/api/dummy_key/stremio/stream/movie/tt1234567.json")
        assert res_default.status_code == 200
        stream_def = res_default.json()["streams"][0]
        assert "https://stremio.example.com" in stream_def["url"]

        # LAN endpoint rewrites stream URL to LAN base URL
        res_lan = client.get("/api/dummy_key/stremio-lan/stream/movie/tt1234567.json")
        assert res_lan.status_code == 200
        stream_lan = res_lan.json()["streams"][0]
        assert stream_lan["url"].startswith(
            "http://192.168.1.100:7070/api/dummy_key/stream/test_token"
        )

        # Tailscale endpoint rewrites stream URL to Tailscale base URL
        res_ts = client.get(
            "/api/dummy_key/stremio-tailscale/stream/movie/tt1234567.json"
        )
        assert res_ts.status_code == 200
        stream_ts = res_ts.json()["streams"][0]
        assert stream_ts["url"].startswith(
            "http://100.64.0.1:7070/api/dummy_key/stream/test_token"
        )

    finally:
        app.dependency_overrides.clear()

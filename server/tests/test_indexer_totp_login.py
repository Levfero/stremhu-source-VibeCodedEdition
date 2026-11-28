"""Tracker login with a 2FA (TOTP) secret: the code sent to the tracker comes from the stored secret.

Trackers often hand out 16-character base32 secrets (80 bits). pyotp 2.12+ rejects secrets under
112 bits, which would turn every such 2FA login into "Érvénytelen 2FA titkos kulcs", so the pin in
requirements.txt stays below that until the call site opts out of the check.
"""

import time

import httpx
import pyotp
import pytest

from app.modules.indexer_definitions.base_indexer_definition import (
    BaseIndexerDefinition,
)
from app.modules.indexer_definitions.exceptions import AuthenticationException
from app.modules.indexer_definitions.schemas.internal import (
    IndexerDefinitionFindTorrentsResult,
    IndexerDefinitionLogin,
    IndexerDefinitionLoginPayload,
)

EIGHTY_BIT_SECRET = "JBSWY3DPEHPK3PXP"


class _RecordingIndexer(BaseIndexerDefinition):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: list[IndexerDefinitionLoginPayload] = []

    @property
    def id(self) -> str:
        return "totp"

    @property
    def name(self) -> str:
        return "TOTP Tracker"

    @property
    def url(self) -> str:
        return "https://totp.invalid"

    @property
    def login_path(self) -> str:
        return "/login"

    @property
    def details_path(self) -> str:
        return "/details/{torrent_id}"

    @property
    def requires_full_download(self) -> bool:
        return False

    def _detect_authentication_error(self, response):
        return None

    async def _login(self, payload: IndexerDefinitionLoginPayload) -> httpx.Response:
        self.payloads.append(payload)
        return httpx.Response(200, request=httpx.Request("POST", self.url + self.login_path))

    async def _fetch_torrents(self, imdb_id, page=None):
        return IndexerDefinitionFindTorrentsResult(torrents=[])

    async def _fetch_torrent(self, torrent_id):
        return None

    async def _fetch_hit_and_run_ids(self):
        return []


@pytest.mark.anyio
async def test_eighty_bit_totp_secret_produces_the_current_code():
    indexer = _RecordingIndexer()
    try:
        await indexer.login(IndexerDefinitionLogin(username="user", password="pw", totp_secret=EIGHTY_BIT_SECRET))
    finally:
        await indexer._client.aclose()

    [payload] = indexer.payloads
    totp = pyotp.TOTP(EIGHTY_BIT_SECRET)
    now = int(time.time())
    assert payload.totp_code in {totp.at(now + step) for step in (-30, 0, 30)}


@pytest.mark.anyio
async def test_invalid_totp_secret_is_rejected_before_login():
    indexer = _RecordingIndexer()
    try:
        with pytest.raises(AuthenticationException):
            await indexer.login(IndexerDefinitionLogin(username="user", password="pw", totp_secret="not base32!"))
    finally:
        await indexer._client.aclose()

    assert indexer.payloads == []

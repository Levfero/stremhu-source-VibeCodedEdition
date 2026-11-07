import ipaddress
import os
import sys
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from rich.console import Console

from app.common.validators import normalize_browser_origin, validate_domain


class NodeEnv(str, Enum):
    DEV = "dev"
    PROD = "prod"


class Config(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(
            Path(__file__).resolve().parent / ".env",
            Path.cwd() / ".env",
        ),
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    node_env: NodeEnv = NodeEnv.PROD
    version: str = "0.0.0"
    description: str = "Torrentalapú streaming magyar torrentoldalakra építve."
    session_secret: str = Field(min_length=32, repr=False)
    session_allowed_origins: list[str] = Field(default_factory=list, repr=False)
    host_ip: str = Field(
        default="",
    )

    reverse_proxy_domain: str | None = None
    tmdb_api_key: str | None = None

    port: int = 7070
    relay_piece_timeout_seconds: float = Field(default=120.0, gt=0)
    relay_read_timeout_seconds: float = Field(default=15.0, gt=0)
    # Felirat: ASS->VTT konverzióra és memóriába gyűjtésre engedett max. forrásméret
    subtitle_max_source_bytes: int = Field(default=8 * 1024 * 1024, gt=0)
    # Feliratcache összesített bájtkerete (folyamaton belüli memória)
    subtitle_cache_max_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    # Indexerről letöltött .torrent dekódolt törzsének felső határa.
    # 48 MiB: a G15-ben natívan elfogadott legnagyobb metaadat (41,9 MB) felett;
    # felhasználói döntés 2026-11-07 (CR15a).
    indexer_torrent_max_bytes: int = Field(default=48 * 1024 * 1024, gt=0)
    # CR15/c (27) forráskeresési keret. Felhasználói UX/SLO-döntés 2026-11-07 (burst27-v3 után):
    # 12 egyidejű egyedi keresés, 15 s teljes határidő, 1 GiB-os konténer.
    source_search_max_parallel: int = Field(default=12, ge=1)
    source_search_deadline_seconds: float = Field(default=15.0, gt=0)
    # Az 1 GiB-os keretből levezetve (burst27-v3, burst27-diag-phase-cost): egy mentési köteg
    # legfeljebb 32 MiB nyers .torrent (a feldolgozás ~4x átmeneti memóriát visz), a letöltött,
    # még nem mentett bájtok folyamatszinten legfeljebb 64 MiB-ig indítanak új letöltést.
    source_search_batch_max_bytes: int = Field(default=32 * 1024 * 1024, gt=0)
    source_search_pending_max_bytes: int = Field(default=64 * 1024 * 1024, gt=0)
    # Indexerenként egyszerre beengedett letöltés: az adapterek alapértelmezett 5-ös szemaforja.
    source_search_downloads_per_indexer: int = Field(default=5, ge=1)
    # Feldolgozott TorrentInfo-gyorsítótár fájlbejegyzés-kerete (~1,3 KiB/fájl, ~65 MiB).
    torrent_info_cache_max_files: int = Field(default=50_000, gt=0)

    @field_validator("session_secret")
    @classmethod
    def validate_session_secret(cls, value: str) -> str:
        if value.strip() == "stremhu-source" or not value.strip():
            raise ValueError("A SESSION_SECRET erős, egyedi aláírókulcs legyen.")
        return value

    @field_validator("session_allowed_origins")
    @classmethod
    def validate_session_allowed_origins(cls, values: list[str]) -> list[str]:
        for value in values:
            if normalize_browser_origin(value)[0] != "https":
                raise ValueError("Csak explicit HTTPS böngésző-origin engedélyezhető.")
        return values

    @property
    def libtorrent_port(self) -> int:
        return 6881

    @property
    def root_dir(self) -> Path:
        return Path(__file__).resolve().parent.parent

    @property
    def base_data_dir(self) -> Path:
        return self.root_dir / "data"

    @property
    def openapi_dir(self) -> Path:
        return self.root_dir / "openapi"

    @property
    def client_path(self) -> Path:
        return self.root_dir / "client"

    @property
    def downloads_dir(self) -> Path:
        return self.base_data_dir / "downloads"

    @property
    def system_dir(self) -> Path:
        return self.base_data_dir / "system"

    @property
    def database_dir(self) -> Path:
        return self.system_dir / "database"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_dir}/app.db"

    @property
    def certificates_dir(self) -> Path:
        return self.system_dir / "certificates"

    acme_directory_url: str = "https://acme-v02.api.letsencrypt.org/directory"

    @model_validator(mode="after")
    def validate_host_ip_and_domain(self) -> "Config":
        if not self.reverse_proxy_domain and not self.host_ip:
            raise ValueError(
                "A `HOST_IP` megadása kötelező, ha a `REVERSE_PROXY_DOMAIN` nincs beállítva!"
            )

        if self.host_ip:
            try:
                ip = ipaddress.ip_address(self.host_ip)
            except ValueError:
                raise ValueError(
                    "A megadott `HOST_IP` formátuma érvénytelen! Kérlek érvényes IPv4 címet adj meg!"
                )

            if ip.is_loopback:
                raise ValueError(
                    "A megadott `HOST_IP` nem lehet a localhost!"
                )

        return self

    @field_validator("reverse_proxy_domain")
    @classmethod
    def validate_reverse_proxy_domain(cls, value: str | None) -> str | None:
        if value is None:
            return value

        return validate_domain(value)


def load_config(*, load_dotenv: bool | None = None, **values: Any) -> Config:
    """Load settings through an explicit, dotenv-free test boundary when requested."""
    if load_dotenv is None:
        load_dotenv = os.environ.get("STREMHU_LOAD_DOTENV", "true").lower() not in {
            "false",
            "0",
            "no",
        }
    if not load_dotenv:
        # Public BaseSettings argument, verified against the installed version.
        values["_env_file"] = None
    return Config(**values)


try:
    config = load_config()
except ValidationError as e:
    console = Console()
    console.print(
        "\n[bold red]‼️  Konfigurációs hiba történt az indítás során:[/bold red]\n"
    )
    for error in e.errors(include_input=False, include_context=False):
        field_path = " -> ".join(str(loc) for loc in error["loc"])
        msg = error["msg"]

        if msg.startswith("Value error, "):
            msg = msg[len("Value error, ") :]

        if field_path:
            console.print(f"  [bold yellow]- {field_path}:[/bold yellow] {msg}")
        else:
            console.print(f"  [bold yellow]-[/bold yellow] {msg}")
    console.print(
        "\n[bold]Kérlek javítsd a fenti hibákat, majd indítsd újra az alkalmazást![/bold]\n"
        "További információ: [blue]https://stremhu.app[/blue]\n"
    )
    sys.exit(1)


def show_internal_routes() -> bool:
    return config.node_env == NodeEnv.DEV


def is_public_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return not (ip.is_private or ip.is_loopback or ip.is_link_local)
    except ValueError:
        return False

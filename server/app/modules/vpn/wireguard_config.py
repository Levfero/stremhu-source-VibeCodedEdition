"""WireGuard `.conf` feldolgozás és validálás (VPN-terv Rev. 5, 6.1).

A hibaüzenetek soha nem tartalmaznak értéket a konfigurációból, csak hibakódot
és legfeljebb a mező nevét.
"""

import base64
import binascii
import hashlib
import ipaddress
import re
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.modules.settings.schemas.internal import VpnConfigMetadata

MAX_CONFIG_BYTES = 16 * 1024

_INTERFACE_KEYS = {"privatekey", "address", "dns", "mtu"}
_PEER_KEYS = {"publickey", "presharedkey", "endpoint", "allowedips", "persistentkeepalive"}
# wg-quick parancsfuttatás és útválasztás: a wireproxy nem használja, és nem is fogadjuk el.
_FORBIDDEN_KEYS = {"postup", "postdown", "preup", "predown", "table", "saveconfig", "fwmark", "listenport"}
_HOSTNAME = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")


class WireGuardConfigError(ValueError):
    """Érvénytelen konfiguráció; az üzenet titokmentes."""

    def __init__(self, code: str, field_name: str | None = None):
        self.code = code
        self.field_name = field_name
        super().__init__(code if field_name is None else f"{code}: {field_name}")


@dataclass(frozen=True)
class WireGuardConfig:
    private_key: str = field(repr=False)
    addresses: tuple[str, ...]
    dns: tuple[str, ...]
    peer_public_key: str
    endpoint: str
    allowed_ips: tuple[str, ...]
    preshared_key: str | None = field(default=None, repr=False)
    persistent_keepalive: int | None = None
    mtu: int | None = None

    def metadata(self) -> VpnConfigMetadata:
        raw = base64.b64decode(self.private_key)
        public = X25519PrivateKey.from_private_bytes(raw).public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
        return VpnConfigMetadata(
            addresses=list(self.addresses),
            dns=list(self.dns),
            endpoint=self.endpoint,
            peer_public_key=self.peer_public_key,
            client_public_key_fingerprint=hashlib.sha256(public).hexdigest()[:16],
        )


def _key(value: str, name: str) -> str:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise WireGuardConfigError("invalid-key", name) from None
    if len(raw) != 32:
        raise WireGuardConfigError("invalid-key", name)
    return value


def _list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _int(value: str, name: str, low: int, high: int) -> int:
    if not value.isdigit():
        raise WireGuardConfigError("invalid-number", name)
    number = int(value)
    if not low <= number <= high:
        raise WireGuardConfigError("invalid-number", name)
    return number


def _endpoint(value: str) -> str:
    match = re.fullmatch(r"\[([0-9A-Fa-f:.]+)\]:(\d{1,5})|([^:\s]+):(\d{1,5})", value)
    if not match:
        raise WireGuardConfigError("invalid-endpoint", "Endpoint")
    host = match.group(1) or match.group(3) or ""
    port = _int(match.group(2) or match.group(4) or "", "Endpoint", 1, 65535)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOSTNAME.match(host):
            raise WireGuardConfigError("invalid-endpoint", "Endpoint") from None
    return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"


def parse_wireguard_config(text: str) -> WireGuardConfig:
    """Egyetlen `[Interface]` és egyetlen `[Peer]` szekciós WireGuard-konfiguráció."""
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise WireGuardConfigError("too-large")
    if any(ord(char) < 32 and char not in "\r\n\t" for char in text):
        raise WireGuardConfigError("invalid-characters")

    sections: dict[str, dict[str, str]] = {}
    current: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1].strip().lower()
            if current not in ("interface", "peer"):
                raise WireGuardConfigError("unknown-section", line[1:-1].strip()[:32])
            if current in sections:
                raise WireGuardConfigError(
                    "multiple-peers" if current == "peer" else "duplicate-section", current
                )
            sections[current] = {}
            continue
        if current is None or "=" not in line:
            raise WireGuardConfigError("malformed-line")
        name, value = (part.strip() for part in line.split("=", 1))
        key = name.lower()
        allowed = _INTERFACE_KEYS if current == "interface" else _PEER_KEYS
        if key in _FORBIDDEN_KEYS:
            raise WireGuardConfigError("unsupported-key", name[:32])
        if key not in allowed:
            raise WireGuardConfigError("unknown-key", name[:32])
        if key in sections[current]:
            raise WireGuardConfigError("duplicate-key", name[:32])
        sections[current][key] = value

    interface = sections.get("interface")
    peer = sections.get("peer")
    if interface is None or peer is None:
        raise WireGuardConfigError("missing-section", "Interface" if interface is None else "Peer")
    for section, required in ((interface, ("privatekey", "address")), (peer, ("publickey", "endpoint", "allowedips"))):
        for key in required:
            if not section.get(key):
                raise WireGuardConfigError("missing-key", key)

    try:
        addresses = tuple(str(ipaddress.ip_interface(item)) for item in _list(interface["address"]))
        dns = tuple(str(ipaddress.ip_address(item)) for item in _list(interface.get("dns", "")))
        allowed_ips = tuple(str(ipaddress.ip_network(item, strict=False)) for item in _list(peer["allowedips"]))
    except ValueError:
        raise WireGuardConfigError("invalid-address") from None
    if not addresses or not allowed_ips:
        raise WireGuardConfigError("invalid-address")

    return WireGuardConfig(
        private_key=_key(interface["privatekey"], "PrivateKey"),
        addresses=addresses,
        dns=dns,
        peer_public_key=_key(peer["publickey"], "PublicKey"),
        endpoint=_endpoint(peer["endpoint"]),
        allowed_ips=allowed_ips,
        preshared_key=_key(peer["presharedkey"], "PresharedKey") if peer.get("presharedkey") else None,
        persistent_keepalive=(
            _int(peer["persistentkeepalive"], "PersistentKeepalive", 0, 65535)
            if peer.get("persistentkeepalive")
            else None
        ),
        mtu=_int(interface["mtu"], "MTU", 576, 9000) if interface.get("mtu") else None,
    )


def check_alive_targets(config: WireGuardConfig, override: list[str] | None = None) -> tuple[str, ...]:
    """A tunnelen át pingelt címek (G6): a megadott lista, különben a konfiguráció DNS-címei."""
    if override:
        return tuple(str(ipaddress.ip_address(item.strip())) for item in override)
    return config.dns


def render_wireproxy_config(
    config: WireGuardConfig,
    socks_port: int,
    username: str,
    password: str,
    check_alive: tuple[str, ...] = (),
    check_alive_interval: int = 5,
) -> str:
    """A wireproxy futási konfigurációja: WireGuard + hitelesített, loopbackre kötött SOCKS5.

    A `CheckAlive` címeket a wireproxy a tunnelen át pingeli; a `/readyz` csak akkor
    ad 200-at, ha válaszolnak. Nélküle a `/readyz` mindig 200 (nem bizonyít kapcsolatot).
    """
    interface = [
        "[Interface]",
        f"PrivateKey = {config.private_key}",
        f"Address = {', '.join(config.addresses)}",
    ]
    if config.dns:
        interface.append(f"DNS = {', '.join(config.dns)}")
    if config.mtu is not None:
        interface.append(f"MTU = {config.mtu}")
    if check_alive:
        interface.append(f"CheckAlive = {', '.join(check_alive)}")
        interface.append(f"CheckAliveInterval = {check_alive_interval}")
    peer = [
        "[Peer]",
        f"PublicKey = {config.peer_public_key}",
        f"Endpoint = {config.endpoint}",
        f"AllowedIPs = {', '.join(config.allowed_ips)}",
    ]
    if config.preshared_key:
        peer.append(f"PresharedKey = {config.preshared_key}")
    if config.persistent_keepalive is not None:
        peer.append(f"PersistentKeepalive = {config.persistent_keepalive}")
    socks = [
        "[Socks5]",
        f"BindAddress = 127.0.0.1:{socks_port}",
        f"Username = {username}",
        f"Password = {password}",
    ]
    return "\n".join([*interface, "", *peer, "", *socks, ""])

import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from app.modules.vpn.wireguard_config import (
    WireGuardConfigError,
    check_alive_targets,
    parse_wireguard_config,
    render_wireproxy_config,
)


def _keys() -> tuple[str, str]:
    private = X25519PrivateKey.generate()
    raw_private = private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    raw_public = private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw_private).decode(), base64.b64encode(raw_public).decode()


@pytest.fixture
def proton_like():
    client_private, _ = _keys()
    _, peer_public = _keys()
    text = f"""[Interface]
# Key for fixture
# Bouncing = 7
# NetShield = 0
PrivateKey = {client_private}
Address = 10.2.0.2/32
DNS = 10.2.0.1

[Peer]
# HU#35
PublicKey = {peer_public}
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = 192.0.2.10:51820
"""
    return text, client_private, peer_public


def test_proton_like_config_parses_with_public_metadata_only(proton_like):
    text, private, peer_public = proton_like
    config = parse_wireguard_config(text)
    metadata = config.metadata()
    assert config.addresses == ("10.2.0.2/32",)
    assert config.dns == ("10.2.0.1",)
    assert config.endpoint == "192.0.2.10:51820"
    assert config.allowed_ips == ("0.0.0.0/0", "::/0")
    assert metadata.peer_public_key == peer_public
    assert len(metadata.client_public_key_fingerprint) == 16
    assert private not in repr(config)
    assert private not in metadata.model_dump_json()


def test_runtime_config_binds_authenticated_socks_on_loopback_only(proton_like):
    text, private, _ = proton_like
    rendered = render_wireproxy_config(parse_wireguard_config(text), 41234, "user-x", "pass-y")
    assert "[Socks5]" in rendered
    assert "BindAddress = 127.0.0.1:41234" in rendered
    assert "Username = user-x" in rendered and "Password = pass-y" in rendered
    assert f"PrivateKey = {private}" in rendered


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda text: text.replace("[Peer]", "[Peers]"), "unknown-section"),
        (lambda text: text + "\n[Peer]\nPublicKey = x\n", "multiple-peers"),
        (lambda text: text.replace("DNS = 10.2.0.1", "PostUp = iptables -F"), "unsupported-key"),
        (lambda text: text.replace("DNS = 10.2.0.1", "Colour = blue"), "unknown-key"),
        (lambda text: text.replace("Endpoint = 192.0.2.10:51820", "Endpoint = 192.0.2.10"), "invalid-endpoint"),
        (lambda text: text.replace("Address = 10.2.0.2/32", "Address = 10.2.0.300/32"), "invalid-address"),
        (lambda text: text.split("[Peer]")[0], "missing-section"),
    ],
)
def test_invalid_configs_are_rejected_with_codes_and_no_secret_values(proton_like, mutate, code):
    text, private, _ = proton_like
    with pytest.raises(WireGuardConfigError) as raised:
        parse_wireguard_config(mutate(text))
    assert raised.value.code == code
    assert private not in str(raised.value)


def test_malformed_private_key_never_echoes_the_value(proton_like):
    text, private, _ = proton_like
    broken = private[:-4] + "!!!="
    with pytest.raises(WireGuardConfigError) as raised:
        parse_wireguard_config(text.replace(private, broken))
    assert raised.value.code == "invalid-key"
    assert broken not in str(raised.value)


def test_check_alive_defaults_to_config_dns_and_can_be_overridden(proton_like):
    text, _, _ = proton_like
    config = parse_wireguard_config(text)
    assert check_alive_targets(config) == ("10.2.0.1",)
    assert check_alive_targets(config, ["10.2.0.9"]) == ("10.2.0.9",)
    rendered = render_wireproxy_config(config, 41234, "u", "p", check_alive=("10.2.0.1",), check_alive_interval=7)
    assert "CheckAlive = 10.2.0.1" in rendered and "CheckAliveInterval = 7" in rendered
    assert "CheckAlive" not in render_wireproxy_config(config, 41234, "u", "p")
    with pytest.raises(ValueError):
        check_alive_targets(config, ["not-an-ip"])

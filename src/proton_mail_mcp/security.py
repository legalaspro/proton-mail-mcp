"""Stored Bridge credentials and the narrowly scoped Bridge TLS policy."""

import hashlib
import hmac
import ipaddress
import os
import socket
import ssl
from dataclasses import dataclass, field
from pathlib import Path

from . import bridge_password, credential_store
from .config import Settings


class CredentialError(RuntimeError):
    """Credentials are unavailable; underlying diagnostic text is intentionally omitted."""


class EmptyCredentialError(CredentialError):
    """The store answered with an empty Bridge password, which a retry cannot change."""


class BridgeSecurityError(RuntimeError):
    """Bridge TLS or endpoint validation failed."""


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str = field(repr=False)


def get_credentials(settings: Settings) -> Credentials:
    """Use a test environment override, otherwise read the configured credential store."""
    password = os.environ.get("PROTON_BRIDGE_PASSWORD")
    if password is None:
        try:
            password = credential_store.lookup(
                settings.credential_store,
                credential_store.BRIDGE_SERVICE,
                settings.bridge_user,
                credential_store.pass_locations(settings.credential_locations),
            )
        except credential_store.CredentialStoreError:
            raise CredentialError(bridge_password.missing(settings.credential_store)) from None
    if not password:
        raise EmptyCredentialError(bridge_password.missing(settings.credential_store, empty=True))
    return Credentials(settings.bridge_user, password)


def _local_address(host: str) -> str | None:
    try:
        address = ipaddress.ip_address(host)
        return str(address) if address.is_loopback else None
    except ValueError:
        if host.lower() != "localhost":
            return None
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        ips = [ipaddress.ip_address(entry[4][0]) for entry in addresses]
        if not ips or not all(address.is_loopback for address in ips):
            raise BridgeSecurityError("Local Bridge hostname must resolve exclusively to loopback")
        # Prefer the IPv4 address used by Bridge's default listener.
        return str(next((address for address in ips if address.version == 4), ips[0]))
    except (OSError, ValueError):
        raise BridgeSecurityError("Cannot resolve the local Bridge endpoint") from None


def bridge_endpoint(settings: Settings) -> str:
    """Connect to a numeric address for localhost, keeping TLS relaxation local."""
    return _local_address(settings.imap_host) or settings.imap_host


def tls_context(settings: Settings) -> ssl.SSLContext:
    """Require STARTTLS; only a loopback Bridge may have an unverified certificate."""
    local = _local_address(settings.imap_host) is not None
    try:
        if settings.imap_cert_path:
            context = ssl.create_default_context(cafile=settings.imap_cert_path)
            context.check_hostname = not local
        elif local:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        else:
            context = ssl.create_default_context()
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context
    except (OSError, ssl.SSLError):
        raise BridgeSecurityError("Cannot load the Bridge certificate") from None


def verify_certificate(connection: object, settings: Settings) -> None:
    """When configured, require the peer's exact leaf certificate before authentication."""
    if not settings.imap_cert_path:
        return
    try:
        pem = Path(settings.imap_cert_path).read_text(encoding="ascii")
        expected = ssl.PEM_cert_to_DER_cert(pem)
        peer = connection.sock.getpeercert(binary_form=True)
        if not peer or not hmac.compare_digest(
            hashlib.sha256(expected).digest(), hashlib.sha256(peer).digest()
        ):
            raise BridgeSecurityError("Bridge certificate does not match the configured pin")
    except (OSError, ValueError, UnicodeError, AttributeError):
        raise BridgeSecurityError("Cannot validate the Bridge certificate pin") from None

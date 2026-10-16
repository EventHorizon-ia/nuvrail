"""
TLS verification on the registration IMAP probe.

POST /api/v1/agents checks the upstream password by logging in over IMAPS
before it stores the credential. That login carries the user's real mailbox
password, so the TLS session must verify the server certificate and hostname.

    probe ──TLS──> upstream IMAP
      │
      ├─ cert untrusted / wrong host ─> SSLCertVerificationError
      │                                 └─> 422 imap_tls_verify_failed
      │                                     (never "wrong password")
      └─ cert trusted (system store or SSL_CERT_FILE) ─> LOGIN / SELECT

The handshake tests run a real one-shot TLS listener on 127.0.0.1 with a
throwaway self-signed certificate, so they exercise the stdlib code path the
probe actually uses rather than only the flags we pass to it.
"""
from __future__ import annotations

import datetime
import imaplib
import ipaddress
import socket
import ssl
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import pytest

from api.routes import auth as auth_routes

# ---------------------------------------------------------------------------
# Context flags: the probe asks for a verifying context
# ---------------------------------------------------------------------------


class _FakeImap:
    """Stands in for imaplib.IMAP4_SSL and records how it was constructed."""

    last_kwargs: ClassVar[dict[str, object]] = {}

    def __init__(self, host: str, port: int, **kwargs: object) -> None:
        type(self).last_kwargs = dict(kwargs)

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return "OK", [b"logged in"]

    def select(self, mailbox: str) -> tuple[str, list[bytes]]:
        return "OK", [b"1"]

    def logout(self) -> tuple[str, list[bytes]]:
        return "BYE", [b""]


def test_probe_passes_a_verifying_ssl_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_routes.imaplib, "IMAP4_SSL", _FakeImap)

    auth_routes._verify_imap_connection_sync("imap.example.com", 993, "me@example.com", "pw")

    ctx = _FakeImap.last_kwargs.get("ssl_context")
    assert isinstance(ctx, ssl.SSLContext), "probe must pass an explicit ssl_context"
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


# ---------------------------------------------------------------------------
# Error mapping: a cert failure is reported as a cert failure
# ---------------------------------------------------------------------------


async def test_cert_failure_maps_to_tls_verify_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_cert_error(*args: object) -> None:
        raise ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate")

    monkeypatch.setattr(auth_routes, "_verify_imap_connection_sync", _raise_cert_error)

    with pytest.raises(auth_routes.ImapValidationError) as excinfo:
        await auth_routes._verify_imap_connection("imap.example.com", 993, "me@example.com", "pw")

    assert excinfo.value.error == "imap_tls_verify_failed"
    assert "TLS certificate verification failed for imap.example.com" in excinfo.value.detail
    assert "password" not in excinfo.value.detail.lower()


async def test_other_ssl_errors_keep_the_generic_mapping(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_ssl_error(*args: object) -> None:
        raise ssl.SSLError(1, "wrong version number")

    monkeypatch.setattr(auth_routes, "_verify_imap_connection_sync", _raise_ssl_error)

    with pytest.raises(auth_routes.ImapValidationError) as excinfo:
        await auth_routes._verify_imap_connection("imap.example.com", 993, "me@example.com", "pw")

    assert excinfo.value.error == "imap_ssl_error"


# ---------------------------------------------------------------------------
# Real handshake against a throwaway self-signed listener
# ---------------------------------------------------------------------------


@pytest.fixture
def self_signed_cert(tmp_path: Path) -> tuple[Path, Path]:
    """Write a self-signed cert valid for IP 127.0.0.1 and return (cert, key)."""
    x509 = pytest.importorskip("cryptography.x509")
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "nuvrail-test-imap")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "cert.pem"
    key_path = tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def tls_listener(self_signed_cert: tuple[Path, Path]) -> Iterator[int]:
    """One-shot TLS server on 127.0.0.1: handshake, send an IMAP greeting, close.

    It never answers LOGIN, so a client that gets past TLS sees the connection
    drop (imaplib.IMAP4.abort) instead of a password ever being checked.
    """
    cert_path, key_path = self_signed_cert
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert_path, key_path)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.settimeout(10)
    port = sock.getsockname()[1]

    def _serve() -> None:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        try:
            with server_ctx.wrap_socket(conn, server_side=True) as tls:
                tls.sendall(b"* OK test server ready\r\n")
                tls.recv(1024)
        except (ssl.SSLError, OSError):
            pass  # expected when the client rejects our certificate

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    try:
        yield port
    finally:
        sock.close()
        thread.join(timeout=5)


def test_probe_rejects_untrusted_certificate(
    tls_listener: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)

    with pytest.raises(ssl.SSLCertVerificationError):
        auth_routes._verify_imap_connection_sync("127.0.0.1", tls_listener, "me@example.com", "pw")


def test_probe_trusts_private_ca_via_ssl_cert_file(
    tls_listener: int,
    self_signed_cert: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cert_path, _ = self_signed_cert
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_path))

    # TLS succeeds; the listener then hangs up instead of answering, which is
    # a connection error, not a certificate error.
    with pytest.raises(imaplib.IMAP4.abort):
        auth_routes._verify_imap_connection_sync("127.0.0.1", tls_listener, "me@example.com", "pw")

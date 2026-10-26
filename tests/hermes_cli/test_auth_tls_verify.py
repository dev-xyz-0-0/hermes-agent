"""Tests for hermes_cli.auth's TLS verify resolution.

``httpx`` deprecated passing ``verify=<str>`` (a CA bundle path) directly --
it now expects an ``ssl.SSLContext``. These tests guard against regressing
back to the deprecated string form.
"""

import datetime
import ssl
import warnings

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from hermes_cli.auth import _ca_bundle_to_ssl_context, _resolve_verify


@pytest.fixture(autouse=True)
def _clear_ca_env(monkeypatch):
    # _resolve_verify falls back to HERMES_CA_BUNDLE/SSL_CERT_FILE; some tests
    # (e.g. test_ssl_certs.py) mutate os.environ directly rather than through
    # monkeypatch, which can leak SSL_CERT_FILE into later tests on the same
    # xdist worker. Keep this test isolated from that regardless of order.
    monkeypatch.delenv("HERMES_CA_BUNDLE", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)


@pytest.fixture()
def real_cafile(tmp_path):
    """A real, parseable self-signed cert -- ssl.create_default_context()
    rejects anything that isn't valid PEM, so a placeholder string won't do."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
        )
        .sign(key, hashes.SHA256())
    )
    cafile = tmp_path / "ca.pem"
    cafile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cafile


class TestResolveVerify:
    def test_insecure_returns_false(self):
        assert _resolve_verify(insecure=True, ca_bundle=None, auth_state=None) is False

    def test_no_ca_bundle_returns_true(self):
        assert _resolve_verify(insecure=False, ca_bundle=None, auth_state=None) is True

    def test_ca_bundle_returns_ssl_context_not_str(self, real_cafile):
        result = _resolve_verify(insecure=False, ca_bundle=str(real_cafile), auth_state=None)
        assert isinstance(result, ssl.SSLContext)
        assert not isinstance(result, str)

    def test_insecure_takes_priority_over_ca_bundle(self, real_cafile):
        assert _resolve_verify(insecure=True, ca_bundle=str(real_cafile), auth_state=None) is False

    def test_auth_state_tls_settings_used_when_no_override(self):
        auth_state = {"tls": {"insecure": True, "ca_bundle": None}}
        assert _resolve_verify(insecure=None, ca_bundle=None, auth_state=auth_state) is False


class TestCaBundleToSslContext:
    def test_file_path_builds_context_without_warning(self, real_cafile):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            context = _ca_bundle_to_ssl_context(str(real_cafile))
        assert isinstance(context, ssl.SSLContext)

    def test_directory_path_uses_capath(self, tmp_path):
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            context = _ca_bundle_to_ssl_context(str(tmp_path))
        assert isinstance(context, ssl.SSLContext)

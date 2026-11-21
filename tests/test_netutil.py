import ssl

from foodapp import netutil


def _paths(cafile=None, capath=None):
    return ssl.DefaultVerifyPaths(cafile=cafile, capath=capath, openssl_cafile_env="SSL_CERT_FILE", openssl_cafile=cafile or "",
                                  openssl_capath_env="SSL_CERT_DIR", openssl_capath=capath or "")


def test_falls_back_to_system_bundle_when_default_is_missing(monkeypatch, tmp_path):
    bundle = tmp_path / "cert.pem"
    bundle.write_text("x")
    original = ssl._create_default_https_context
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _paths("/nonexistent/cert.pem", "/nonexistent/certs"))
    monkeypatch.setattr(netutil, "_CANDIDATES", (str(tmp_path / "missing.pem"), str(bundle)))
    monkeypatch.setattr(ssl, "_create_default_https_context", original)  # restored by monkeypatch afterwards
    used = []
    monkeypatch.setattr(ssl, "create_default_context", lambda *a, **k: used.append(k.get("cafile")) or object())
    assert netutil.install_ca_fallback() == str(bundle)
    ssl._create_default_https_context()  # what urllib calls for each HTTPS request
    assert used == [str(bundle)]  # verification stays on: a normal verifying context, built from the system bundle


def test_does_nothing_when_default_bundle_exists(monkeypatch, tmp_path):
    bundle = tmp_path / "cert.pem"
    bundle.write_text("x")
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.delenv("SSL_CERT_DIR", raising=False)
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _paths(str(bundle)))
    assert netutil.install_ca_fallback() is None


def test_respects_a_bundle_the_user_chose(monkeypatch):
    monkeypatch.setenv("SSL_CERT_FILE", "/somewhere/else.pem")
    assert netutil.install_ca_fallback() is None

"""HTTPS certificate fallback for Python builds that ship without CA certificates.

The python.org macOS installer of Python has no certificate bundle until its "Install Certificates.command" is run, so every
urllib HTTPS request fails with CERTIFICATE_VERIFY_FAILED (the MBTA, routing and geocoding calls all use urllib). When the
default bundle is missing we point urllib at the operating system's bundle instead. Certificate verification stays ON; it is
never disabled.
"""

import os
import ssl

_CANDIDATES = (
    "/etc/ssl/cert.pem",                           # macOS system bundle
    "/opt/homebrew/etc/ca-certificates/cert.pem",  # Homebrew (Apple silicon)
    "/usr/local/etc/ca-certificates/cert.pem",     # Homebrew (Intel)
    "/etc/ssl/certs/ca-certificates.crt",          # Debian / Ubuntu
    "/etc/pki/tls/certs/ca-bundle.crt",            # Fedora / RHEL
)


def install_ca_fallback():
    """Use the system certificate bundle when Python's default one is missing. Returns the bundle path used, or None."""
    paths = ssl.get_default_verify_paths()
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return None  # the user already chose a bundle
    if paths.cafile and os.path.exists(paths.cafile):
        return None
    if paths.capath and os.path.isdir(paths.capath) and os.listdir(paths.capath):
        return None
    for candidate in _CANDIDATES:
        if os.path.exists(candidate):
            ssl._create_default_https_context = lambda *args, _cafile=candidate, **kwargs: ssl.create_default_context(cafile=_cafile)
            return candidate
    return None

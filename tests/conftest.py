import pytest

# API keys and certificate settings from a developer's shell or .env must not change
# what the suite exercises (the live-provider code paths turn on when a key is set).
AMBIENT = ("MAPTILER_API_KEY", "MBTA_API_KEY", "ORS_API_KEY", "SSL_CERT_FILE", "SSL_CERT_DIR")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in AMBIENT:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def no_outside_network(monkeypatch):
    """The suite never talks to MBTA, routing or map services: a test that reaches them would be slow, flaky
    offline and in CI. Connections to this machine (the test servers) still work."""
    import socket
    real = socket.create_connection

    def guarded(address, *args, **kwargs):
        if address[0] not in ("127.0.0.1", "localhost", "::1"):
            raise OSError(f"network access is blocked in tests: {address[0]}")
        return real(address, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", guarded)

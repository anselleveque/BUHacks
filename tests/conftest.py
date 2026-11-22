import pytest

# API keys and certificate settings from a developer's shell or .env must not change
# what the suite exercises (the live-provider code paths turn on when a key is set).
AMBIENT = ("MAPTILER_API_KEY", "MBTA_API_KEY", "ORS_API_KEY", "SSL_CERT_FILE", "SSL_CERT_DIR")


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in AMBIENT:
        monkeypatch.delenv(name, raising=False)

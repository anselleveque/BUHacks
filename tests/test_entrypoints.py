"""Command-line startup checks without binding a socket."""

import sqlite3
from contextlib import closing
from unittest.mock import MagicMock

import pytest

from foodapp import server
from foodapp.demo import __main__ as demo_entrypoint
from foodapp.demo import server as demo_server


@pytest.mark.parametrize("option", [
    ["--bind", "0.0.0.0"], ["--routes", "routes.json"], ["--scenario", "hero"],
    ["--frozen"], ["--fresh"], ["--director-host", "demo.local"], ["--no-dev"],
])
def test_demo_only_options_require_demo(option, capsys):
    with pytest.raises(SystemExit) as error:
        server.parse_args(option)
    assert error.value.code == 2
    assert f"{option[0]} requires --demo" in capsys.readouterr().err
    assert server.parse_args([*option, "--demo"])[1].demo


def test_demo_rejects_user_db_option(capsys):
    with pytest.raises(SystemExit) as error:
        server.main(["--demo", "--user-db", "data/user.db"])
    assert error.value.code == 2
    assert "--user-db cannot be used with --demo" in capsys.readouterr().err


@pytest.fixture
def catalog(tmp_path):
    path = tmp_path / "catalog.db"
    with closing(sqlite3.connect(path)) as db:
        db.execute("CREATE TABLE meta (value TEXT)")
    return path


@pytest.fixture
def demo_factory(monkeypatch):
    instance = MagicMock()

    def make_server(address, db, **options):
        instance.__enter__.return_value = instance
        instance.server_port = address[1]
        instance.demo_dir = options["data_dir"]
        instance.user_db_path = instance.demo_dir / "user.db"
        instance.dev_mode = options["dev_mode"]
        return instance

    factory = MagicMock(side_effect=make_server)
    monkeypatch.setattr(demo_server, "DemoServer", factory)
    return factory, instance


@pytest.mark.parametrize("entrypoint, flags, port", [
    (server.main, ["--demo"], 8000),
    (demo_entrypoint.main, [], 8800),
])
def test_demo_startup_defaults(entrypoint, flags, port, catalog, demo_factory, capsys):
    factory, instance = demo_factory
    entrypoint([*flags, "--db", str(catalog)])
    factory.assert_called_once_with(
        ("127.0.0.1", port), catalog, data_dir=demo_server.DEMO_DIR,
        routes_path=demo_server.ROUTES_PATH, scenario="1", running=True,
        extra_hosts=[], director_hosts=[], resume=True, dev_mode=True,
    )
    instance.serve_forever.assert_called_once_with()
    banner = capsys.readouterr().out
    assert f"app http://127.0.0.1:{port}/  Director http://127.0.0.1:{port}/demo/" in banner
    assert f"User database: {demo_server.DEMO_DIR / 'user.db'}" in banner
    assert (
        "Demo mode. Press Ctrl+Alt+Shift+D (Cmd+Option+Shift+D on Mac) in the app "
        "for the clock and scenarios."
    ) in banner
    assert ("Tip: python3 -m foodapp.server --demo" in banner) == (port == 8800)


def test_demo_startup_overrides(catalog, demo_factory, tmp_path):
    factory, instance = demo_factory
    data_dir = tmp_path / "demo"
    routes = tmp_path / "routes.json"
    server.main([
        "--demo", "--db", str(catalog), "--data-dir", str(data_dir), "--routes", str(routes),
        "--fresh", "--scenario", "hero", "--frozen", "--port", "8123", "--bind", "0.0.0.0",
        "--allow-host", "app.local", "--director-host", "director.local", "--no-dev",
    ])
    factory.assert_called_once_with(
        ("0.0.0.0", 8123), catalog, data_dir=data_dir, routes_path=routes,
        scenario="hero", running=False, extra_hosts=["app.local"],
        director_hosts=["director.local"], resume=False, dev_mode=False,
    )
    instance.serve_forever.assert_called_once_with()


def test_normal_startup(catalog, demo_factory, monkeypatch, capsys):
    factory = MagicMock()
    instance = factory.return_value.__enter__.return_value
    instance.db_uri = catalog.as_uri() + "?mode=ro"
    instance.server_port = 8000
    load_env = MagicMock()
    monkeypatch.setattr(server, "Server", factory)
    monkeypatch.setattr(server, "load_env", load_env)

    server.main(["--db", str(catalog)])

    user_db = server.ROOT / "data" / "user.db"
    load_env.assert_called_once_with(server.ROOT / ".env")
    factory.assert_called_once_with(
        ("127.0.0.1", 8000), catalog, user_db=user_db, allow_now=False,
        data_dir=None, extra_hosts=[], dev_mode=False,
    )
    demo_factory[0].assert_not_called()
    instance.serve_forever.assert_called_once_with()
    assert capsys.readouterr().out.splitlines() == [
        "Midnight Munch is listening on http://127.0.0.1:8000",
        f"User database: {user_db}",
    ]

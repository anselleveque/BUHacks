"""Start the demo server: python3 -m foodapp.demo --port 8800 (runbook: docs/DEMO.md)."""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from foodapp.demo.fixtures import ROUTES_PATH
from foodapp.demo.server import DEFAULT_DB, DEMO_DIR, DemoServer
from foodapp.server import serving_authorities


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8800)
    parser.add_argument("--bind", default="127.0.0.1",
                        help="Listen address. Use 0.0.0.0 to reach the demo from a phone on the same network.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="The food database (opened read-only).")
    parser.add_argument("--data-dir", type=Path, default=DEMO_DIR,
                        help="Demo user database and uploads, rebuilt at start (default data/demo).")
    parser.add_argument("--routes", type=Path, default=ROUTES_PATH, help="Recorded walking routes.")
    parser.add_argument("--scenario", default="1", help="Preset to start with (1 to 9 or its slug).")
    parser.add_argument("--frozen", action="store_true", help="Start with the clock frozen.")
    parser.add_argument("--allow-host", action="append", default=[], metavar="HOST[:PORT]",
                        help="Also serve the app (and its POSTs) on this host, for example your LAN address.")
    parser.add_argument("--director-host", action="append", default=[], metavar="HOST[:PORT]",
                        help="Also accept Director controls on this host. Anyone who can reach it can drive the demo.")
    args = parser.parse_args(argv)
    try:
        serving_authorities(args.port, args.allow_host)
        serving_authorities(args.port, args.director_host)
    except ValueError as error:
        parser.error(str(error))
    try:
        with closing(sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("SELECT 1 FROM meta LIMIT 1").fetchone()
    except sqlite3.Error:
        parser.error("Cannot open the food database. Run python3 -m foodapp.build_db.")
    try:
        server = DemoServer((args.bind, args.port), args.db, data_dir=args.data_dir, routes_path=args.routes,
                            scenario=args.scenario, running=not args.frozen, extra_hosts=args.allow_host,
                            director_hosts=args.director_host)
    except KeyError:
        parser.error("Unknown scenario. Use 1 to 9 or a slug such as hero.")
    except ValueError as error:
        parser.error(str(error))
    with server:
        port = server.server_port
        print(f"Midnight Munch demo: app http://127.0.0.1:{port}/  Director http://127.0.0.1:{port}/demo/", flush=True)
        print(f"Walking routes recorded: {len(server.runtime.walks)} (others are estimates).", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

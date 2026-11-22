import http.client
import json
import socket
import tempfile
import threading
import unittest
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

from mock_api import Handler, MockCommunity


class MockCommunityPersistenceTests(unittest.TestCase):
    def request(self, server, path, payload):
        client, peer = socket.socketpair()

        def handle():
            with peer:
                Handler(peer, ("localhost", 0), server)

        worker = threading.Thread(target=handle, daemon=True)
        worker.start()
        try:
            with closing(http.client.HTTPConnection("localhost", timeout=5)) as connection:
                connection.sock = client
                client.settimeout(5)
                connection.request("POST", path, json.dumps(payload), {
                    "Origin": "http://localhost", "Content-Type": "application/json",
                })
                response = connection.getresponse()
                return response.status, json.loads(response.read()), response.headers
        finally:
            worker.join(5)

    def test_api_changes_persist_and_reset_is_dev_gated(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mock-user.db"
            server = SimpleNamespace(
                args=SimpleNamespace(dev=False),
                community=MockCommunity(path),
                community_lock=Lock(),
                now_source=None,
            )
            status, _, headers = self.request(server, "/api/favorite", {
                "place_id": 101, "favorite": True,
            })
            self.assertEqual(status, 200)
            reporter = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]

            payload = {"confirm": True, "keep_community": False, "keep_photos": True, "keep_browser": True}
            status, response, _ = self.request(server, "/api/dev-reset", payload)
            self.assertEqual((status, response["error"]), (403, "Start the mock server with --dev to reset saved data."))

            server.args.dev = True
            status, response, _ = self.request(server, "/api/dev-reset", payload)
            self.assertEqual(status, 200)
            self.assertTrue(response["ok"])
            restarted = MockCommunity(path)
            self.assertEqual(restarted.favorites, {})
            self.assertNotIn(reporter, restarted.favorites)

    def test_state_survives_reload_and_reset_retains_selected_data(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "mock-user.db"
            community = MockCommunity(path)
            reporter = "reporter-1"
            community.visits[reporter] = {
                101: {"place_id": 101, "name": "Commonwealth Bowl", "visited_at": "2026-11-22T00:00:00-05:00"}
            }
            community.favorites[reporter] = {
                "Commonwealth Bowl": {"place_id": 101, "name": "Commonwealth Bowl"}
            }
            community.logs[reporter] = [
                {"place_id": 101, "name": "Commonwealth Bowl", "photo_url": "/api/visit-photo/photo-1"}
            ]
            community.reports[(1, reporter)] = (1200, 100.0)
            community.photos["photo-1"] = (reporter, "image/jpeg", b"photo bytes")
            community.item_reports[("Commonwealth Bowl", "spicy tofu", reporter)] = (
                "remove", datetime.fromisoformat("2026-11-22T00:00:00+00:00")
            )
            community.item_report_counts[(reporter, date(2026, 11, 22))] = 2
            community.persist()

            restored = MockCommunity(path)
            self.assertEqual(restored.visits[reporter][101]["name"], "Commonwealth Bowl")
            self.assertEqual(restored.favorites[reporter]["Commonwealth Bowl"]["place_id"], 101)
            self.assertEqual(restored.logs[reporter][0]["photo_url"], "/api/visit-photo/photo-1")
            self.assertEqual(restored.reports[(1, reporter)], (1200, 100.0))
            self.assertEqual(restored.photos["photo-1"], (reporter, "image/jpeg", b"photo bytes"))
            self.assertEqual(restored.item_reports[("Commonwealth Bowl", "spicy tofu", reporter)][0], "remove")
            self.assertEqual(restored.item_report_counts[(reporter, date(2026, 11, 22))], 2)

            deleted = restored.reset(keep_community=True, keep_photos=False)
            self.assertEqual(deleted["photos"], 1)
            self.assertIn(reporter, restored.visits)
            self.assertIsNone(restored.logs[reporter][0]["photo_url"])
            restored.persist()
            after_reset = MockCommunity(path)
            self.assertIn(reporter, after_reset.visits)
            self.assertEqual(after_reset.photos, {})

            after_reset.photos["photo-2"] = (reporter, "image/jpeg", b"another photo")
            after_reset.reset(keep_community=False, keep_photos=True)
            after_reset.persist()
            cleared = MockCommunity(path)
            self.assertEqual(cleared.visits, {})
            self.assertEqual(cleared.logs, {})
            self.assertEqual(cleared.photos["photo-2"], (reporter, "image/jpeg", b"another photo"))


if __name__ == "__main__":
    unittest.main()

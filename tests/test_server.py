import json
import http.client
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import ApiHandler, DashboardHandler, EventStore, LiveSnapshot, ValidationError, _load_assets, _make_server, validate_payload


class ValidationTests(unittest.TestCase):
    def test_accepts_numeric_values_and_sql_like_text_as_data(self):
        payload = {
            "events": [
                {
                    "name": "Japan CPI ' OR 1=1 --",
                    "country": "Japan",
                    "date": "2026-11-27",
                    "time": "18:30",
                    "actual": 103.3,
                    "forecast": None,
                    "previous": "102.9",
                }
            ]
        }
        rows = validate_payload(payload)
        self.assertEqual(rows[0][0], "Japan CPI ' OR 1=1 --")
        self.assertEqual(rows[0][4:], ("103.3", "", "102.9"))

    def test_rejects_bad_dates_extra_fields_and_duplicates(self):
        base = {"name": "CPI", "date": "2026-02-30", "time": "18:30"}
        with self.assertRaises(ValidationError):
            validate_payload({"events": [base]})
        with self.assertRaises(ValidationError):
            validate_payload({"events": [dict(base, command="ignored")]})
        valid = dict(base, date="2026-02-28")
        with self.assertRaises(ValidationError):
            validate_payload({"events": [valid, valid]})


class StoreTests(unittest.TestCase):
    def test_replaces_and_reads_events_using_bound_values(self):
        payload = {
            "events": [
                {
                    "name": "CPI'); DROP TABLE events; --",
                    "date": "2026-11-27",
                    "time": "18:30",
                    "actual": "3.1%",
                }
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            store = EventStore(Path(directory) / "events.sqlite3")
            store.replace_events(validate_payload(payload))
            stored = store.list_events()
            self.assertEqual(stored[0]["name"], payload["events"][0]["name"])
            self.assertEqual(stored[0]["actual"], "3.1%")
            store.replace_events([])
            self.assertEqual(store.list_events(), [])


class LiveSnapshotTests(unittest.TestCase):
    def test_publishes_to_memory_before_persisting(self):
        class PersistenceProbe:
            def list_events(self):
                return []

            def replace_events(self, rows):
                self.observed_snapshot = self.state.snapshot()

        store = PersistenceProbe()
        state = LiveSnapshot(store)
        store.state = state
        rows = validate_payload(
            {"events": [{"name": "CPI", "date": "2026-11-27", "time": "18:30"}]}
        )

        state.update(rows)

        self.assertEqual(store.observed_snapshot["events"][0]["name"], "CPI")
        self.assertFalse(store.observed_snapshot["persisted"])
        self.assertTrue(state.snapshot()["persisted"])

    def test_keeps_publish_and_persist_status_events_in_order(self):
        class ImmediateStore:
            def list_events(self):
                return []

            def replace_events(self, rows):
                pass

        state = LiveSnapshot(ImmediateStore())
        subscriber, _ = state.subscribe()
        rows = validate_payload(
            {"events": [{"name": "CPI", "date": "2026-11-27", "time": "18:30"}]}
        )

        state.update(rows)

        self.assertFalse(subscriber.get_nowait()["persisted"])
        self.assertTrue(subscriber.get_nowait()["persisted"])


class ApiHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        store = EventStore(Path(self.temporary_directory.name) / "events.sqlite3")
        ApiHandler.state = LiveSnapshot(store)
        ApiHandler.token = "test-token-with-more-than-32-characters"
        ApiHandler.allowed_origins = frozenset({"https://client.example"})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), ApiHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = "http://127.0.0.1:{}/api/updates".format(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary_directory.cleanup()

    def test_requires_token_and_replaces_snapshot(self):
        body = json.dumps(
            {"events": [{"name": "CPI", "date": "2026-11-27", "time": "18:30"}]}
        ).encode("utf-8")
        unauthorized = Request(
            self.url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(HTTPError) as error:
            urlopen(unauthorized)
        self.assertEqual(error.exception.code, 401)
        error.exception.close()

        authorized = Request(
            self.url,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + ApiHandler.token,
                "Origin": "https://client.example",
            },
            method="POST",
        )
        with urlopen(authorized) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://client.example")
            self.assertEqual(json.loads(response.read()), {"ok": True, "count": 1})
        self.assertEqual(ApiHandler.state.snapshot()["events"][0]["name"], "CPI")

    def test_allows_private_network_preflight_for_configured_origin(self):
        preflight = Request(
            self.url,
            headers={
                "Origin": "https://client.example",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
                "Access-Control-Request-Private-Network": "true",
            },
            method="OPTIONS",
        )
        with urlopen(preflight) as response:
            self.assertEqual(response.status, 204)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://client.example")
            self.assertEqual(response.headers["Access-Control-Allow-Private-Network"], "true")


class DashboardStreamTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        store = EventStore(Path(self.temporary_directory.name) / "events.sqlite3")
        DashboardHandler.state = LiveSnapshot(store)
        DashboardHandler.assets = _load_assets()
        DashboardHandler.allowed_origins = frozenset({"https://pages.example"})
        self.server = _make_server("127.0.0.1", 0, DashboardHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.connection = http.client.HTTPConnection(
            "127.0.0.1", self.server.server_address[1], timeout=3
        )

    def tearDown(self):
        self.connection.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary_directory.cleanup()

    def _read_event(self, response):
        data = None
        while True:
            line = response.readline()
            if not line:
                self.fail("event stream closed unexpectedly")
            if line.startswith(b"data: "):
                data = line[6:].strip()
            elif line == b"\n" and data is not None:
                return json.loads(data)

    def test_dashboard_data_allows_configured_pages_origin(self):
        request = Request(
            "http://127.0.0.1:{}/dashboard-data".format(self.server.server_address[1]),
            headers={"Origin": "https://pages.example"},
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers["Access-Control-Allow-Origin"], "https://pages.example")

    def test_dashboard_serves_runtime_api_config(self):
        request = Request(
            "http://127.0.0.1:{}/config.js".format(self.server.server_address[1])
        )
        with urlopen(request) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"ECONOMIC_API_BASE_URL", response.read())

    def test_stream_pushes_snapshot_to_dashboard(self):
        self.connection.request(
            "GET",
            "/live-events",
            headers={"Origin": "https://pages.example"},
        )
        response = self.connection.getresponse()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.getheader("Access-Control-Allow-Origin"), "https://pages.example")
        self.assertEqual(self._read_event(response)["events"], [])

        rows = validate_payload(
            {"events": [{"name": "Japan CPI", "date": "2026-11-27", "time": "18:30"}]}
        )
        DashboardHandler.state.update(rows)

        pushed_snapshot = self._read_event(response)
        self.assertEqual(pushed_snapshot["events"][0]["name"], "Japan CPI")


if __name__ == "__main__":
    unittest.main()

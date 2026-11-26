import hmac
import json
import logging
import math
import os
import queue
import re
import sqlite3
import threading
from contextlib import closing
from datetime import date, datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unicodedata import category
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DATABASE = DATA_DIR / "economic_events.sqlite3"
MAX_BODY_BYTES = 1024 * 1024
MAX_EVENTS = 1000
ALLOWED_FIELDS = {"name", "country", "date", "time", "actual", "forecast", "previous"}
VALUE_FIELDS = {"actual", "forecast", "previous"}
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
LOG = logging.getLogger("economic_server")


class ValidationError(ValueError):
    pass


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("invalid JSON number")


def _text(value, field, max_length, required=False):
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ValidationError(field + " must be a string")
    value = value.strip()
    if required and not value:
        raise ValidationError(field + " cannot be empty")
    if len(value) > max_length:
        raise ValidationError(field + " is too long")
    if any(category(char) in {"Cc", "Cs"} for char in value):
        raise ValidationError(field + " contains control characters")
    return value


def _event_value(value, field):
    if value is None:
        return ""
    if isinstance(value, bool):
        raise ValidationError(field + " must be a string or number")
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValidationError(field + " must be finite")
        value = str(value)
    return _text(value, field, 64)


def validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"events"}:
        raise ValidationError("request must contain only an events array")
    events = payload["events"]
    if not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValidationError("events must be an array of at most 1000 items")

    normalized = []
    seen = set()
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise ValidationError("each event must be an object")
        if set(event) - ALLOWED_FIELDS:
            raise ValidationError("event contains an unsupported field")
        if not {"name", "date", "time"}.issubset(event):
            raise ValidationError("each event requires name, date, and time")

        name = _text(event["name"], "name", 160, required=True)
        country = _text(event.get("country"), "country", 80)
        event_date = _text(event["date"], "date", 10, required=True)
        event_time = _text(event["time"], "time", 5, required=True)
        if not DATE_PATTERN.fullmatch(event_date):
            raise ValidationError("date must use YYYY-MM-DD format")
        try:
            date.fromisoformat(event_date)
        except ValueError as exc:
            raise ValidationError("date is not a valid calendar date") from exc
        if not TIME_PATTERN.fullmatch(event_time):
            raise ValidationError("time must use 24-hour HH:MM format")

        key = (name, country, event_date, event_time)
        if key in seen:
            raise ValidationError("events contain a duplicate")
        seen.add(key)
        normalized.append(
            (
                name,
                country,
                event_date,
                event_time,
                _event_value(event.get("actual"), "actual"),
                _event_value(event.get("forecast"), "forecast"),
                _event_value(event.get("previous"), "previous"),
            )
        )
    return normalized


class EventStore:
    def __init__(self, path=DATABASE):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        name TEXT NOT NULL,
                        country TEXT NOT NULL,
                        event_date TEXT NOT NULL,
                        event_time TEXT NOT NULL,
                        actual TEXT NOT NULL,
                        forecast TEXT NOT NULL,
                        previous TEXT NOT NULL,
                        PRIMARY KEY (name, country, event_date, event_time)
                    )
                    """
                )

    def replace_events(self, events):
        with closing(sqlite3.connect(self.path)) as connection:
            with connection:
                connection.execute("DELETE FROM events")
                connection.executemany(
                    """
                    INSERT INTO events
                        (name, country, event_date, event_time, actual, forecast, previous)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    events,
                )

    def list_events(self):
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                """
                SELECT name, country, event_date, event_time, actual, forecast, previous
                FROM events
                ORDER BY event_date, event_time, country, name
                """
            ).fetchall()
        return [
            {
                "name": row[0],
                "country": row[1],
                "date": row[2],
                "time": row[3],
                "actual": row[4] or None,
                "forecast": row[5] or None,
                "previous": row[6] or None,
            }
            for row in rows
        ]


class LiveSnapshot:
    def __init__(self, event_store):
        self.event_store = event_store
        self._lock = threading.Lock()
        self._update_lock = threading.Lock()
        self._events = event_store.list_events()
        self._persisted = True
        self._updated_at = datetime.now(timezone.utc).isoformat()
        self._subscribers = set()

    def _snapshot_locked(self):
        return {
            "events": [dict(event) for event in self._events],
            "persisted": self._persisted,
            "updated_at": self._updated_at,
        }

    def snapshot(self):
        with self._lock:
            return self._snapshot_locked()

    def subscribe(self):
        subscriber = queue.Queue(maxsize=2)
        with self._lock:
            self._subscribers.add(subscriber)
            initial_snapshot = self._snapshot_locked()
        return subscriber, initial_snapshot

    def unsubscribe(self, subscriber):
        with self._lock:
            self._subscribers.discard(subscriber)

    def _notify_locked(self):
        snapshot = self._snapshot_locked()
        for subscriber in self._subscribers:
            try:
                subscriber.put_nowait(snapshot)
            except queue.Full:
                try:
                    subscriber.get_nowait()
                except queue.Empty:
                    pass
                subscriber.put_nowait(snapshot)

    def update(self, rows):
        events = [
            {
                "name": row[0],
                "country": row[1],
                "date": row[2],
                "time": row[3],
                "actual": row[4] or None,
                "forecast": row[5] or None,
                "previous": row[6] or None,
            }
            for row in rows
        ]
        events.sort(key=lambda event: (event["date"], event["time"], event["country"], event["name"]))

        with self._update_lock:
            with self._lock:
                self._events = events
                self._persisted = False
                self._updated_at = datetime.now(timezone.utc).isoformat()
                self._notify_locked()

            self.event_store.replace_events(rows)

            with self._lock:
                self._persisted = True
                self._notify_locked()


class SecureHandler(BaseHTTPRequestHandler):
    server_version = "EconomicEvents"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, format_string, *args):
        LOG.info("%s handled a request", self.client_address[0])

    def _send(self, status, body=b"", content_type="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if extra:
            for name, value in extra.items():
                self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)
        self.close_connection = True

    def _json(self, status, payload, extra=None):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, extra=extra)

    def _not_found(self):
        self._json(404, {"error": "not found"})

    def do_GET(self):
        self._not_found()

    def do_POST(self):
        self._json(405, {"error": "method not allowed"}, {"Allow": "GET"})


class ApiHandler(SecureHandler):
    state = None
    token = ""
    allowed_origins = frozenset()

    def _origin_headers(self):
        origin = self.headers.get("Origin")
        if origin is None:
            return None, {}
        if origin not in self.allowed_origins:
            return origin, None
        headers = {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Max-Age": "600",
            "Vary": "Origin",
        }
        if self.headers.get("Access-Control-Request-Private-Network", "").lower() == "true":
            headers["Access-Control-Allow-Private-Network"] = "true"
        return origin, headers

    def do_OPTIONS(self):
        if urlsplit(self.path).path != "/api/updates":
            self._not_found()
            return
        origin, headers = self._origin_headers()
        if headers is None:
            self._json(403, {"error": "origin is not allowed"})
            return
        self._send(204, extra=headers)

    def do_POST(self):
        if urlsplit(self.path).path != "/api/updates":
            self._not_found()
            return
        origin, cors_headers = self._origin_headers()
        if cors_headers is None:
            self._json(403, {"error": "origin is not allowed"})
            return

        authorization = self.headers.get("Authorization", "")
        scheme, separator, supplied_token = authorization.partition(" ")
        if (
            scheme.lower() != "bearer"
            or not separator
            or not hmac.compare_digest(supplied_token.encode("utf-8"), self.token.encode("utf-8"))
        ):
            self._json(401, {"error": "unauthorized"}, cors_headers)
            return

        if self.headers.get("Transfer-Encoding"):
            self._json(400, {"error": "transfer encoding is not supported"}, cors_headers)
            return
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._json(415, {"error": "content type must be application/json"}, cors_headers)
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._json(400, {"error": "invalid content length"}, cors_headers)
            return
        if length < 0:
            self._json(400, {"error": "invalid content length"}, cors_headers)
            return
        if length > MAX_BODY_BYTES:
            self._json(413, {"error": "request body is too large"}, cors_headers)
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._json(400, {"error": "incomplete request body"}, cors_headers)
            return
        try:
            payload = json.loads(
                body.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
            events = validate_payload(payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ValidationError) else "invalid JSON request"
            self._json(400, {"error": message}, cors_headers)
            return
        try:
            self.state.update(events)
        except sqlite3.Error:
            LOG.exception("Failed to save event update")
            self._json(500, {"error": "could not save events"}, cors_headers)
            return
        self._json(200, {"ok": True, "count": len(events)}, cors_headers)

    def do_GET(self):
        self._json(405, {"error": "method not allowed"}, {"Allow": "POST, OPTIONS"})


class DashboardHandler(SecureHandler):
    state = None
    assets = {}
    allowed_origins = frozenset()

    def _cors_headers(self):
        origin = self.headers.get("Origin")
        if origin in self.allowed_origins:
            return {
                "Access-Control-Allow-Origin": origin,
                "Vary": "Origin",
            }
        return {}

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/dashboard-data":
            self._json(200, self.state.snapshot(), self._cors_headers())
            return
        if path == "/live-events":
            self._stream_live_events()
            return
        if path == "/":
            asset = "index.html"
            content_type = "text/html; charset=utf-8"
        elif path == "/app.js":
            asset = "app.js"
            content_type = "text/javascript; charset=utf-8"
        elif path == "/config.js":
            asset = "config.js"
            content_type = "text/javascript; charset=utf-8"
        elif path == "/styles.css":
            asset = "styles.css"
            content_type = "text/css; charset=utf-8"
        else:
            self._not_found()
            return
        body = self.assets[asset]
        self._send(
            200,
            body,
            content_type,
            {
                "Content-Security-Policy": (
                    "default-src 'none'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
                ),
                "X-Frame-Options": "DENY",
            },
        )

    def _stream_live_events(self):
        subscriber, initial_snapshot = self.state.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in self._cors_headers().items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(b"retry: 3000\n\n")
            self._write_event(initial_snapshot)
            while True:
                try:
                    snapshot = subscriber.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                    continue
                self._write_event(snapshot)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.state.unsubscribe(subscriber)
            self.close_connection = True

    def _write_event(self, snapshot):
        data = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.wfile.write(b"data: " + data + b"\n\n")
        self.wfile.flush()

    def do_POST(self):
        self._json(405, {"error": "method not allowed"}, {"Allow": "GET"})


def _load_assets():
    static_dir = ROOT / "static"
    return {
        filename: (static_dir / filename).read_bytes()
        for filename in ("index.html", "app.js", "config.js", "styles.css")
    }


def _make_server(host, port, handler):
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("ECONOMIC_API_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("Set ECONOMIC_API_TOKEN to a secret of at least 32 characters.")
    origins = frozenset(
        origin.strip()
        for origin in os.environ.get("ECONOMIC_ALLOWED_ORIGINS", "").split(",")
        if origin.strip()
    )
    for origin in origins:
        parsed = urlsplit(origin)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment:
            raise SystemExit("ECONOMIC_ALLOWED_ORIGINS must contain origins only, e.g. https://example.com")

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    store = EventStore()
    state = LiveSnapshot(store)
    host = os.environ.get("ECONOMIC_BIND_HOST", "0.0.0.0")
    api_port = int(os.environ.get("ECONOMIC_API_PORT", "443"))
    dashboard_port = int(os.environ.get("ECONOMIC_DASHBOARD_PORT", "8080"))

    ApiHandler.state = state
    ApiHandler.token = token
    ApiHandler.allowed_origins = origins
    DashboardHandler.state = state
    DashboardHandler.assets = _load_assets()
    DashboardHandler.allowed_origins = origins

    api_server = _make_server(host, api_port, ApiHandler)
    try:
        dashboard_server = _make_server(host, dashboard_port, DashboardHandler)
    except Exception:
        api_server.server_close()
        raise

    api_thread = threading.Thread(target=api_server.serve_forever, daemon=True)
    api_thread.start()
    LOG.warning(
        "HTTP is unencrypted: bearer tokens and event data are sent in plaintext."
    )
    LOG.info("HTTP ingestion endpoint listening on %s:%s", host, api_port)
    LOG.info("HTTP dashboard listening on %s:%s", host, dashboard_port)
    try:
        dashboard_server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Shutting down")
    finally:
        dashboard_server.shutdown()
        api_server.shutdown()
        dashboard_server.server_close()
        api_server.server_close()


if __name__ == "__main__":
    main()

"""Tiny stdlib HTTP server behind the config editor page."""

from __future__ import annotations

import json
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__
from .fleet import Fleet, check_value, code_version

STATIC_DIR = Path(__file__).parent / "static"
REPO_DIR = Path(__file__).resolve().parents[1]
MAX_BODY = 1 << 20


class EditorHandler(BaseHTTPRequestHandler):
    server_version = "ConfigEditor/" + __version__

    def __init__(self, *args, fleet: Fleet, lock: threading.Lock, backup_done: set,
                 version: str, **kw):
        self.fleet = fleet
        self.lock = lock
        self.backup_done = backup_done
        self.version = version
        super().__init__(*args, **kw)

    # --- routing ---------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send_page()
            elif path == "/api/state":
                self._send_state()
            else:
                self._send_error(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as exc:  # keep one bad request from killing the server
            self._send_error(500, "%s: %s" % (type(exc).__name__, exc))

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if not self._same_origin():
                self._send_error(403, "cross-origin request refused")
                return
            body = self._read_json()
            if path == "/api/set":
                self._set(body)
            else:
                self._send_error(404, "not found")
        except BrokenPipeError:
            pass
        except (ValueError, KeyError) as exc:
            self._send_error(400, str(exc))
        except Exception as exc:
            self._send_error(500, "%s: %s" % (type(exc).__name__, exc))

    # --- handlers --------------------------------------------------------

    def _state_payload(self) -> dict:
        if self.fleet.changed_on_disk():
            self.fleet.reload()
        payload = self.fleet.matrix()
        payload["version"] = self.version
        return payload

    def _send_page(self) -> None:
        """index.html with the current state embedded, so the first paint is complete."""
        target = STATIC_DIR / "index.html"
        with self.lock:
            payload = self._state_payload()
        # "</" must not appear inside the inline script; JSON allows the escape.
        blob = json.dumps(payload).replace("</", "<\\/")
        html = target.read_text(encoding="utf-8").replace(
            "/*__STATE__*/null", blob, 1)
        self._respond(200, "text/html; charset=utf-8", html.encode("utf-8"))

    def _send_state(self) -> None:
        with self.lock:
            payload = self._state_payload()
        self._send_json(200, payload)

    def _set(self, body: dict) -> None:
        section = str(body.get("section", "")).strip()
        option = str(body.get("option", "")).strip()
        values = body.get("values")
        if not section or not option or not isinstance(values, dict) or not values:
            raise ValueError("need section, option and a non-empty values map")
        for tid, v in values.items():
            if v is not None and not isinstance(v, str):
                raise ValueError("value for %s must be a string or null" % tid)
        expect = body.get("expect_mtimes") or {}
        kind = self.fleet.types.get(option.lower())
        warnings = {tid: w for tid, v in values.items()
                    if v is not None for w in [check_value(v, kind)] if w}

        with self.lock:
            result = self.fleet.apply(section, option, values, expect_mtimes=expect,
                                      backup_done=self.backup_done)
            payload = self._state_payload()
        payload["result"] = result
        payload["warnings"] = warnings
        self._send_json(409 if result["conflict"] else 200, payload)

    # --- plumbing --------------------------------------------------------

    def _same_origin(self) -> bool:
        """Reject a POST made from another site's page open in the same browser."""
        origin = self.headers.get("Origin")
        if not origin:
            return True  # curl / scripts
        host = self.headers.get("Host", "")
        return origin.rstrip("/") in ("http://" + host, "https://" + host)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            raise ValueError("bad request body size")
        data = self.rfile.read(length)
        body = json.loads(data.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError("JSON object expected")
        return body

    def _send_static(self, name: str, content_type: str) -> None:
        target = STATIC_DIR / name
        if not target.is_file():
            self._send_error(404, "not found")
            return
        self._respond(200, content_type, target.read_bytes())

    def _send_json(self, status: int, payload: dict) -> None:
        self._respond(status, "application/json", json.dumps(payload).encode())

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": message})

    def log_message(self, fmt: str, *args) -> None:
        pass


def serve(fleet: Fleet, host: str, port: int) -> None:
    handler = partial(
        EditorHandler,
        fleet=fleet,
        lock=threading.Lock(),
        backup_done=set(),
        version=code_version(REPO_DIR),
    )
    httpd = ThreadingHTTPServer((host, port), handler)
    shown = host if host not in ("0.0.0.0", "") else "localhost"
    print("Config editor: http://%s:%d" % (shown, port))
    print("Editing %d config(s): %s" % (len(fleet.targets), ", ".join(t.id for t in fleet.targets)))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()

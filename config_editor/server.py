"""Tiny stdlib HTTP server behind the config editor page."""

from __future__ import annotations

import json
import threading
import time
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import __version__, storage
from .fleet import Fleet, check_value, code_version, journal

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
        self.server.last_seen = time.monotonic()
        path = urlparse(self.path).path
        try:
            if path == "/":
                self._send_page()
            elif path == "/api/state":
                self._send_state()
            elif path == "/api/alive":
                self._send_json(200, {})
            elif path == "/api/audit":
                with self.lock:
                    if self.fleet.changed_on_disk():
                        self.fleet.reload()
                    payload = self.fleet.audit()
                self._send_json(200, payload)
            elif path == "/storage":
                self._send_storage_page()
            elif path == "/storage-sim.js":
                self._send_static("storage-sim.js", "text/javascript; charset=utf-8")
            elif path == "/api/storage-sim":
                refresh = "refresh=1" in (urlparse(self.path).query or "")
                with self.lock:
                    if self.fleet.changed_on_disk():
                        self.fleet.reload()
                    storage.measure_in_background(self.fleet, force=refresh)
                    payload = storage.sim_inputs(self.fleet)
                self._send_json(200, payload)
            else:
                self._send_error(404, "not found")
        except BrokenPipeError:
            pass
        except Exception as exc:  # keep one bad request from killing the server
            self._send_error(500, "%s: %s" % (type(exc).__name__, exc))

    def do_POST(self) -> None:  # noqa: N802
        self.server.last_seen = time.monotonic()
        path = urlparse(self.path).path
        try:
            if not self._same_origin():
                self._send_error(403, "cross-origin request refused")
                return
            body = self._read_json()
            if path == "/api/set":
                self._set(body)
            elif path == "/api/migrate":
                self._migrate(body)
            elif path == "/api/dedupe":
                self._dedupe(body)
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
            payload["audit"] = self.fleet.audit()   # so the Audit panel opens instantly
        # "</" must not appear inside the inline script; JSON allows the escape.
        blob = json.dumps(payload).replace("</", "<\\/")
        html = target.read_text(encoding="utf-8").replace(
            "/*__STATE__*/null", blob, 1)
        self._respond(200, "text/html; charset=utf-8", html.encode("utf-8"))

    def _send_storage_page(self) -> None:
        """storage.html with the simulator inputs embedded, like the editor page."""
        with self.lock:
            if self.fleet.changed_on_disk():
                self.fleet.reload()
            storage.measure_in_background(self.fleet)
            payload = storage.sim_inputs(self.fleet)
        blob = json.dumps(payload).replace("</", "<\\/")
        html = (STATIC_DIR / "storage.html").read_text(encoding="utf-8").replace("/*__SIM__*/null", blob, 1)
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
            journal("ui  %s  POST /api/set [%s] %s for %s" % (self.client_address[0], section, option, ", ".join(values)))
            result = self.fleet.apply(section, option, values, expect_mtimes=expect,
                                      backup_done=self.backup_done)
            payload = self._state_payload()
        payload["result"] = result
        payload["warnings"] = warnings
        self._send_json(409 if result["conflict"] else 200, payload)

    def _dedupe(self, body: dict) -> None:
        tid, section, option = (str(body.get(k, "")).strip() for k in ("station", "section", "option"))
        if not tid or not section or not option:
            raise ValueError("need station, section and option")
        with self.lock:
            result = self.fleet.dedupe(tid, section, option, backup_done=self.backup_done)
            payload = self._state_payload()
        payload["result"] = result
        self._send_json(200, payload)

    def _migrate(self, body: dict) -> None:
        ids = body.get("stations")
        apply = bool(body.get("apply", False))
        recent = bool(body.get("recent", False))
        if not isinstance(ids, list) or not ids or not all(isinstance(i, str) for i in ids):
            raise ValueError("need a non-empty list of stations")
        with self.lock:
            result = self.fleet.migrate(ids, apply=apply, recent=recent, backup_done=self.backup_done)
            payload = self._state_payload() if apply else {}
        payload["result"] = result
        self._send_json(200, payload)

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


def _stop_when_idle(httpd: ThreadingHTTPServer, lock: threading.Lock, idle_s: float) -> None:
    """Stop the server once no page has asked it anything for ``idle_s`` seconds.

    An open page keeps polling (a hidden tab at least once a minute), so idle means
    every editor tab is closed. Never mid-measurement; and the lock is taken so a
    write in progress finishes and none starts after the server is gone.
    """
    while True:
        time.sleep(min(60.0, idle_s / 4))
        if time.monotonic() - httpd.last_seen < idle_s or storage.measuring():
            continue
        lock.acquire()
        if time.monotonic() - httpd.last_seen < idle_s:
            lock.release()
            continue
        print("%s no page open for %g min: stopped" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), idle_s / 60),
              flush=True)
        httpd.shutdown()
        return


def serve(fleet: Fleet, host: str, port: int, idle_s: float = 0) -> None:
    """Serve the editor; with ``idle_s`` > 0, exit once no page has been open that long."""
    lock = threading.Lock()
    handler = partial(
        EditorHandler,
        fleet=fleet,
        lock=lock,
        backup_done=set(),
        version=code_version(REPO_DIR),
    )
    storage.measure_in_background(fleet)   # warm the Storage page's per-night sizes
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.last_seen = time.monotonic()
    shown = host if host not in ("0.0.0.0", "") else "localhost"
    print("Config editor: http://%s:%d" % (shown, port))
    print("Editing %d config(s): %s" % (len(fleet.targets), ", ".join(t.id for t in fleet.targets)))
    if idle_s > 0:
        threading.Thread(target=_stop_when_idle, args=(httpd, lock, idle_s), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()

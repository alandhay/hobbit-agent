"""The dashboard: a stdlib HTTP server on 127.0.0.1.

GET /                    the dashboard page
GET /api/events?after=N  events from number N on (long-poll, up to 25 s)
GET /api/state           stats, lessons, map, characters and places, and the runner's status
GET /api/screen.next?after=V  the next emulator frame newer than version V (waits up to 10 s; X-Frame-Version header)
GET /api/screen.stream   the same frames as one multipart stream (for other viewers; Chrome paints it unreliably)
GET /api/live.png        the latest frame
GET /api/frame/<session>/<name>  a frame saved in a session folder
POST /api/control        the operator's buttons: {"action": "reset" | "sleep" | "wake"}, with the
                         operator password (HOBBIT_OPERATOR_PASSWORD) as a Bearer token when it is set
"""

from __future__ import annotations

import hmac
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .archive import EventLog, Store
from .rig import Rig

STATIC = Path(__file__).resolve().parent / "static"
BOUNDARY = "hobbitframe"


def serve(host: str, port: int, events: EventLog, store: Store, rig: Rig, runner) -> ThreadingHTTPServer:
    # Without a password (a local run) anyone who can reach the page may press the buttons; on the
    # server the page is public behind HTTPS, so the deploy always sets one.
    password = os.environ.get("HOBBIT_OPERATOR_PASSWORD", "")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):     # keep the console for the run's own log
            pass

        def _send(self, code: int, body: bytes, content_type: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data) -> None:
            self._send(200, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _stream_screen(self) -> None:
            """The emulator screen as a multipart image stream: an <img> shows each part as it arrives."""
            self.send_response(200)
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            seen = -1
            try:
                # Browsers show a part only once the next boundary arrives, so each frame is
                # followed straight away by a boundary: a frozen game then still shows its picture.
                self.wfile.write(f"--{BOUNDARY}\r\n".encode())
                while True:
                    seen, png = rig.stream.wait_newer(seen, 2.0)
                    if png is None:
                        continue
                    self.wfile.write(f"Content-Type: image/png\r\nContent-Length: {len(png)}\r\n\r\n".encode())
                    self.wfile.write(png + f"\r\n--{BOUNDARY}\r\n".encode())
                    self.wfile.flush()
            except ConnectionError:
                pass

        def do_GET(self) -> None:
            url = urlparse(self.path)
            if url.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "dashboard.html").read_bytes(), "text/html; charset=utf-8")
            if url.path == "/api/events":
                after = int(parse_qs(url.query).get("after", ["0"])[0])
                return self._json(events.since(after, 25.0))
            if url.path == "/api/state":
                return self._json({"status": runner.status, "stats": runner.stats(), "lessons": store.top_lessons(),
                                   "experiments": store.kb.get("experiments", []), "map": store.map,
                                   "entities": store.entities,
                                   "interval": runner.cfg.move_interval_seconds})
            if url.path == "/api/screen.next":
                after = int(parse_qs(url.query).get("after", ["-1"])[0])
                version, png = rig.stream.wait_newer(after, 10.0)
                if png is None:
                    return self._send(204, b"", "image/png")
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.send_header("X-Frame-Version", str(version))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return self.wfile.write(png)
            if url.path == "/api/screen.stream":
                return self._stream_screen()
            if url.path == "/api/live.png":
                png = rig.stream.png if rig.stream else None
                if png is None:
                    return self._send(404, b"no screen yet", "text/plain")
                return self._send(200, png, "image/png")
            m = re.fullmatch(r"/api/frame/([\w-]+)/(\d{6}\.png)", url.path)
            if m:
                path = runner.cfg.sessions / m.group(1) / "frames" / m.group(2)
                if path.is_file():
                    return self._send(200, path.read_bytes(), "image/png")
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:
            if urlparse(self.path).path != "/api/control":
                return self._send(404, b"not found", "text/plain")
            given = self.headers.get("Authorization", "").removeprefix("Bearer ")
            if password and not hmac.compare_digest(given.encode(), password.encode()):
                time.sleep(1.0)                # slows down guessing
                return self._send(401, b'{"error": "wrong operator password"}', "application/json")
            try:
                length = min(int(self.headers.get("Content-Length", "0")), 4096)
                action = json.loads(self.rfile.read(length) or b"{}").get("action", "")
                return self._json(runner.control(action))
            except (ValueError, AttributeError) as exc:
                return self._send(400, json.dumps({"error": str(exc)}).encode(), "application/json")

    class Server(ThreadingHTTPServer):
        def handle_error(self, request, client_address) -> None:
            if not isinstance(sys.exc_info()[1], ConnectionError):    # a closed tab is not worth a traceback
                super().handle_error(request, client_address)

    server = Server((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True, name="hobbit-http").start()
    return server

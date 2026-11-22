"""Web frontend: manage watched routes in a browser, and run the seat check every 6 h.

One Railway service owns the SQLite volume, so the check runs here (as run.py in a
subprocess) instead of in a separate cron service.
"""
import base64
import hmac
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import db
from run import apply_command, now_paris
from stations import STATIONS
from telegram import CommandError, parse_command

log = logging.getLogger("web")
INDEX = Path(__file__).with_name("static").joinpath("index.html")
CHECK_HOURS = 6  # UTC 00/06/12/18, like the old cron; the noon run sends the day-before reminder


def checker() -> None:
    while True:
        now = datetime.now(timezone.utc)
        nxt = now.replace(minute=0, second=0, microsecond=0)
        nxt += timedelta(hours=CHECK_HOURS - nxt.hour % CHECK_HOURS)
        time.sleep((nxt - now).total_seconds())
        subprocess.run([sys.executable, "run.py"], check=False)  # logs and exit code go to Railway


class Handler(BaseHTTPRequestHandler):
    def _authed(self) -> bool:
        want = os.environ["WEB_PASSWORD"]
        try:
            got = base64.b64decode(self.headers.get("Authorization", "")[6:]).decode().partition(":")[2]
        except ValueError:
            got = ""
        return hmac.compare_digest(got.encode(), want.encode())

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if code == 401:
            self.send_header("WWW-Authenticate", 'Basic realm="tgvmax"')
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj).encode())

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        if not self._authed():
            return self._send(401, b"")
        if self.path == "/":
            return self._send(200, INDEX.read_bytes(), "text/html; charset=utf-8")
        if self.path == "/api/routes":
            conn = db.connect()
            try:
                routes = [{**asdict(r), "label": r.label(), "slot": r.slot()} for r in db.list_routes(conn)]
            finally:
                conn.close()
            return self._json(200, {"routes": routes, "stations": sorted(STATIONS)})
        self._send(404, b"")

    def do_POST(self):
        if not self._authed():
            return self._send(401, b"")
        if self.path != "/api/command":
            return self._send(404, b"")
        try:
            text = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)))).get("text", "")
            cmd = parse_command(text, now_paris().date())
            if cmd is None or cmd.name not in ("add", "del"):
                raise CommandError("Seulement /add et /del.")
        except CommandError as e:
            return self._json(400, {"error": str(e)})
        except (ValueError, AttributeError):
            return self._json(400, {"error": "Requête invalide."})
        conn = db.connect()
        try:
            reply, _ = apply_command(conn, cmd)
        finally:
            conn.close()
        self._json(200, {"reply": reply})

    def log_message(self, fmt, *args):  # default logs the Authorization-bearing request line only, keep it quiet
        log.info(fmt, *args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not os.environ.get("WEB_PASSWORD"):
        sys.exit("WEB_PASSWORD must be set")
    threading.Thread(target=checker, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8000))), Handler).serve_forever()


if __name__ == "__main__":
    main()

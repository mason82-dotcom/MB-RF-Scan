"""Loopback + explicitly configured home-LAN passive RF dashboard."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import ipaddress
import threading
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlsplit

from .lab import LabStore, virtual_replay
from .hardware.cc1101_sim import CC1101Simulator
from .hardware.sources import available_sources
from .receiver import Monitor
from .storage import EventStore
from .iq_trace import TraceArchive

WEB = Path(__file__).resolve().parent / "web"


LAN_IP = "192.168.178.63"
LAN_CLIENTS = ipaddress.ip_network("192.168.178.0/24")


def client_allowed(address: str) -> bool:
    """Allow direct local clients and only the specified local IPv4 subnet."""
    try:
        peer = ipaddress.ip_address(address)
    except ValueError:
        return False
    return peer.is_loopback or (
        isinstance(peer, ipaddress.IPv4Address) and peer in LAN_CLIENTS
    )


def serve(host: str = "127.0.0.1", port: int = 8765,
          db_path: str | None = None, *, lan_host: str | None = None):
    # Two explicitly bound listeners, never 0.0.0.0 / wildcard.
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("Loopback host required; use --lan-host for LAN access")
    if lan_host is not None and lan_host != LAN_IP:
        raise ValueError("LAN listener is restricted to 192.168.178.63")
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("Invalid listening port")
    db_path = db_path or str(Path.home() / ".local/share/keyrfmonitor/events.sqlite3")
    store = EventStore(db_path)
    monitor = Monitor(store)
    lab = LabStore(Path(db_path).parent / "synthetic_lab.sqlite3")
    simulator = CC1101Simulator(store.insert)
    csrf = secrets.token_urlsafe(32)
    trace_archive = TraceArchive(Path(db_path).parent / 'iq-traces')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            # No key material is present in paths, but avoid arbitrary query-string logging.
            return

        def _write(self, data: bytes, content_type: str, status: int = 200):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, payload: dict, status: int = 200):
            self._write(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode(),
                        "application/json; charset=utf-8", status)

        def _network_allowed(self) -> bool:
            peer = self.client_address[0]
            # Block unexpected source subnets even if a router forwards traffic
            # to the explicitly bound LAN address. Host check prevents rebinding.
            hostname = (self.headers.get("Host") or "").split(":", 1)[0].lower()
            if not client_allowed(peer):
                self._json({"message": "Zugriff nur aus 192.168.178.0/24 oder lokal"}, 403)
                return False
            if hostname not in ("127.0.0.1", "localhost", LAN_IP):
                self._json({"message": "Ungültiger Host"}, 403)
                return False
            return True

        def do_GET(self):
            if not self._network_allowed():
                return
            if self.path in ("/", "/index.html"):
                html = (WEB / "index.html").read_text(encoding="utf-8")
                self._write(html.replace("__RF_CSRF_TOKEN__", csrf).encode(),
                            "text/html; charset=utf-8")
            elif self.path in ("/app.js", "/styles.css"):
                name = self.path[1:]
                typ = "text/javascript" if name == "app.js" else "text/css"
                self._write((WEB / name).read_bytes(), typ + "; charset=utf-8")
            elif self.path == "/api/status":
                self._json(monitor.status())
            elif self.path == "/api/events":
                self._json({"events": store.recent()})
            elif self.path == "/api/sessions":
                self._json({"sessions": store.recent_sessions(), "source": "cli_scan"})
            elif urlsplit(self.path).path == "/api/session-detail":
                try:
                    parsed = urlsplit(self.path)
                    params = parse_qs(parsed.query, keep_blank_values=True,
                                      strict_parsing=True)
                    if (set(params) != {"id", "bucket"}
                            or len(params["id"]) != 1 or len(params["bucket"]) != 1
                            or not params["id"][0].isascii()
                            or not params["id"][0].isdecimal()
                            or not params["bucket"][0].isascii()
                            or not params["bucket"][0].isdecimal()):
                        raise ValueError("Invalid session detail parameters")
                    session_id = int(params["id"][0])
                    bucket = int(params["bucket"][0])
                    detail = store.session_timeline(session_id, bucket)
                    if detail is None:
                        self._json({"message": "Scan-Sitzung nicht gefunden."}, 404)
                    else:
                        self._json(detail)
                except (ValueError, OverflowError):
                    self._json({"message": "Gültige ID und bucket=5 oder 10 erforderlich."}, 400)
            elif urlsplit(self.path).path == "/api/summary":
                try:
                    query = parse_qs(urlsplit(self.path).query, keep_blank_values=True,
                                     strict_parsing=True)
                    if set(query) - {"hours"} or len(query.get("hours", [])) > 1:
                        raise ValueError("invalid query")
                    hours = int(query["hours"][0]) if "hours" in query else 24
                    self._json(store.summary(hours=hours))
                except (ValueError, OverflowError):
                    self._json({"message": "Zeitfenster: 6, 24 oder 168 Stunden."}, 400)
            elif self.path == "/api/iq/traces":
                self._json({"traces": trace_archive.list_recent(),
                            "capture_mode": "explicit_cli_only", "tx_enabled": False})
            elif urlsplit(self.path).path == "/api/iq/trace":
                try:
                    query = parse_qs(urlsplit(self.path).query,
                                     keep_blank_values=True, strict_parsing=True)
                    if set(query) != {"id"} or len(query["id"]) != 1:
                        raise ValueError("Ungültige Trace-Parameter")
                    entry = trace_archive.read(query["id"][0])
                    self._json({"trace": entry}, 200) if entry else self._json(
                        {"message": "Trace nicht gefunden"}, 404)
                except (ValueError, OverflowError):
                    self._json({"message": "Ungültige Trace-ID"}, 400)
            elif urlsplit(self.path).path == "/api/iq/samples":
                try:
                    query = parse_qs(urlsplit(self.path).query,
                                     keep_blank_values=True, strict_parsing=True)
                    if set(query) != {"id", "start", "count"} or any(len(v) != 1 for v in query.values()):
                        raise ValueError("Ungültige Parameter")
                    start_txt, count_txt = query["start"][0], query["count"][0]
                    if not (start_txt.isascii() and start_txt.isdecimal()
                            and count_txt.isascii() and count_txt.isdecimal()):
                        raise ValueError("Ungültige Zahlen")
                    page = trace_archive.sample_page(query["id"][0], int(start_txt), int(count_txt))
                    self._json(page, 200) if page else self._json(
                        {"message": "Trace nicht gefunden"}, 404)
                except (ValueError, OverflowError):
                    self._json({"message": "Erwartet: id, start und count=512|2048|8192"}, 400)
            elif self.path == "/api/sources":
                self._json({"sources": available_sources()})
            elif self.path == "/api/cc1101/status":
                self._json(simulator.status())
            elif self.path == "/api/lab/codes":
                self._json({"codes": lab.list_codes(), "mode": "virtual_only"})
            else:
                self._json({"error": "Not found"}, 404)

        def do_POST(self):
            if not self._network_allowed():
                return
            if self.headers.get("X-RFMonitor-Token") != csrf:
                self._json({"message": "Ungültiges Zugriffstoken"}, 403)
                return
            if self.path == "/api/start":
                ok, message = monitor.start()
            elif self.path == "/api/stop":
                ok, message = monitor.stop()
            elif self.path == "/api/cc1101/sim/start":
                ok, message = simulator.start()
            elif self.path == "/api/cc1101/sim/stop":
                ok, message = simulator.stop()
            elif self.path == "/api/lab/create":
                try:
                    code = lab.create()
                except ValueError as exc:
                    self._json({"message": str(exc)}, 409)
                    return
                self._json({"message": "Synthetischer Testcode gespeichert.", "code": code}, 201)
                return
            elif self.path == "/api/lab/replay":
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 2 <= size <= 100:
                        raise ValueError("Ungültige Eingabegröße")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict) or set(payload) != {"id"}:
                        raise ValueError("Nur eine gespeicherte Testcode-ID ist erlaubt")
                    code_id = payload["id"]
                    bits = lab.read_bits(code_id)
                    result = virtual_replay(bits)
                    lab.record_run(code_id, result["bursts"], result["pairs"])
                except (ValueError, KeyError, json.JSONDecodeError) as exc:
                    self._json({"message": str(exc)}, 400)
                    return
                self._json({"message": "Virtuelle Wiedergabe beendet. Keine HF ausgesendet.",
                            "result": result})
                return
            else:
                self._json({"message": "Not found"}, 404)
                return
            self._json({"message": message}, 200 if ok else 409)

    httpd = None
    lan_httpd = None
    lan_thread = None
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
        if lan_host:
            # If the LAN address is unavailable, fail without opening a wildcard
            # socket or accidentally closing only one of the listeners.
            lan_httpd = ThreadingHTTPServer((lan_host, port), Handler)
            lan_thread = threading.Thread(
                target=lan_httpd.serve_forever, kwargs={"poll_interval": 0.25},
                name="keyrfmonitor-lan-http", daemon=True)
            lan_thread.start()
            print(f"MB-RF-Scan LAN: http://{lan_host}:{port} "
                  "(Quellnetz 192.168.178.0/24, nur HTTP)", flush=True)
        print(f"KeyRFMonitor: http://{host}:{port} "
              "(lokal, keine SDR-Autostartfunktion)", flush=True)
        httpd.serve_forever(poll_interval=0.25)
    finally:
        if lan_httpd is not None:
            lan_httpd.shutdown()
            lan_httpd.server_close()
        if lan_thread is not None:
            lan_thread.join(timeout=3)
        if httpd is not None:
            httpd.server_close()
        simulator.close()
        monitor.close()
        store.close()
        lab.close()

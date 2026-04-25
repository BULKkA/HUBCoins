"""
Cookie Update Server
====================
Порт: 8765 (COOKIE_SERVER_PORT)
Эндпоинты:
  GET  /              — веб-панель
  GET  /cookie-status — статус куки
  GET  /bot-status    — статус бота
  GET  /logs          — SSE стрим логов
  POST /update-cookies — обновить куки
  POST /bot/pause     — пауза
  POST /bot/resume    — возобновить
"""

import json
import os
import threading
import logging
import collections
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

COOKIES_FILE = os.environ.get("COOKIES_FILE", "data/cookies.json")
SERVER_PORT  = int(os.environ.get("COOKIE_SERVER_PORT", "8765"))
HTML_PATH    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookie_updater.html")

bot_paused  = False
log_buffer  = collections.deque(maxlen=300)
sse_clients = []
_sse_lock   = threading.Lock()


class WebLogHandler(logging.Handler):
    def emit(self, record):
        entry = {
            "t":   datetime.now().strftime("%H:%M:%S"),
            "lvl": record.levelname,
            "msg": record.getMessage(),
        }
        log_buffer.append(entry)
        _push_sse(json.dumps(entry))


def _push_sse(data: str):
    with _sse_lock:
        clients = list(sse_clients)
    dead = []
    for q in clients:
        try:
            q.append(data)
        except Exception:
            dead.append(q)
    if dead:
        with _sse_lock:
            for q in dead:
                try:
                    sse_clients.remove(q)
                except ValueError:
                    pass


def setup_web_logging():
    handler = WebLogHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logging.getLogger().addHandler(handler)


def load_cookies_from_file() -> dict:
    if not os.path.exists(COOKIES_FILE):
        return {}
    try:
        with open(COOKIES_FILE) as f:
            return json.load(f).get("cookies", {})
    except Exception:
        return {}


def save_cookies_to_file(cookie_string: str) -> dict:
    cookies = {}
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()
    os.makedirs(os.path.dirname(os.path.abspath(COOKIES_FILE)), exist_ok=True)
    with open(COOKIES_FILE, "w") as f:
        json.dump({
            "cookies":    cookies,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2, ensure_ascii=False)
    logging.getLogger(__name__).info(
        "Куки обновлены (%d значений)", len(cookies))
    return cookies


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]

        if path in ("/", "/index.html"):
            try:
                with open(HTML_PATH, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self._json(404, {"error": "cookie_updater.html not found"})

        elif path == "/cookie-status":
            status = {"ok": False}
            if os.path.exists(COOKIES_FILE):
                try:
                    with open(COOKIES_FILE) as f:
                        data = json.load(f)
                    status = {"ok": True, "updated_at": data.get("updated_at")}
                except Exception:
                    pass
            self._json(200, status)

        elif path == "/bot-status":
            self._json(200, {"paused": bot_paused})

        elif path == "/logs":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self._cors()
            self.end_headers()
            try:
                for entry in list(log_buffer):
                    self.wfile.write(f"data: {json.dumps(entry)}\n\n".encode())
                self.wfile.flush()
            except Exception:
                return
            q = collections.deque()
            with _sse_lock:
                sse_clients.append(q)
            try:
                while True:
                    if q:
                        msg = q.popleft()
                        self.wfile.write(f"data: {msg}\n\n".encode())
                        self.wfile.flush()
                    else:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        time.sleep(1)
            except Exception:
                pass
            finally:
                with _sse_lock:
                    try:
                        sse_clients.remove(q)
                    except ValueError:
                        pass
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        global bot_paused
        path = self.path.split("?")[0]

        if path == "/update-cookies":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                data = json.loads(body)
                cs = data.get("cookies", "").strip()
                if not cs:
                    self._json(400, {"ok": False, "error": "empty"})
                    return
                save_cookies_to_file(cs)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        elif path == "/bot/pause":
            bot_paused = True
            logging.getLogger(__name__).info("⏸ Бот поставлен на паузу")
            self._json(200, {"ok": True, "paused": True})

        elif path == "/bot/resume":
            bot_paused = False
            logging.getLogger(__name__).info("▶ Бот возобновлён")
            self._json(200, {"ok": True, "paused": False})

        else:
            self._json(404, {"error": "not found"})

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)


def start_cookie_server():
    setup_web_logging()
    server = HTTPServer(("0.0.0.0", SERVER_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.getLogger(__name__).info(
        "🌐 Cookie сервер запущен на порту %d", SERVER_PORT)
    return server


def is_paused() -> bool:
    return bot_paused
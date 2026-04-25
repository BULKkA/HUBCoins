"""
Cookie Update Server
====================
Запускается вместе с фармером.
Принимает куки от букмарклета и сохраняет их в файл.
Фармер читает куки из файла перед каждым запросом.

Порт: 8765 (настраивается через COOKIE_SERVER_PORT)
"""

import json
import os
import threading
import logging
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

log = logging.getLogger(__name__)

COOKIES_FILE  = os.environ.get("COOKIES_FILE", "/app/data/cookies.json")
SERVER_PORT   = int(os.environ.get("COOKIE_SERVER_PORT", "8765"))

# Читаем HTML из файла рядом
HTML_PATH = os.path.join(os.path.dirname(__file__), "cookie_updater.html")


def load_cookies_from_file() -> dict:
    """Загружает куки из файла. Возвращает {} если файла нет."""
    if not os.path.exists(COOKIES_FILE):
        return {}
    try:
        with open(COOKIES_FILE) as f:
            data = json.load(f)
            return data.get("cookies", {})
    except Exception:
        return {}


def save_cookies_to_file(cookie_string: str) -> dict:
    """Парсит строку куки и сохраняет в файл."""
    cookies = {}
    for part in cookie_string.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            cookies[k.strip()] = v.strip()

    os.makedirs(os.path.dirname(COOKIES_FILE), exist_ok=True)
    with open(COOKIES_FILE, "w") as f:
        json.dump({
            "cookies":    cookies,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }, f, indent=2, ensure_ascii=False)

    log.info("Куки обновлены через веб-интерфейс (%d значений)", len(cookies))
    return cookies


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # подавляем стандартные логи сервера

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "https://astanahub.com")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
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

        elif self.path == "/cookie-status":
            status = {"ok": False}
            if os.path.exists(COOKIES_FILE):
                try:
                    with open(COOKIES_FILE) as f:
                        data = json.load(f)
                    status = {"ok": True, "updated_at": data.get("updated_at")}
                except Exception:
                    pass
            self._json(200, status)

        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/update-cookies":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                data   = json.loads(body)
                cookie_string = data.get("cookies", "").strip()
                if not cookie_string:
                    self._json(400, {"ok": False, "error": "empty cookies"})
                    return
                save_cookies_to_file(cookie_string)
                self._json(200, {"ok": True})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})
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
    """Запускает HTTP сервер в фоновом потоке."""
    server = HTTPServer(("0.0.0.0", SERVER_PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    log.info("🌐 Cookie сервер запущен на порту %d", SERVER_PORT)
    return server

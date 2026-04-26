"""
Astana Hub — Coin Farmer
========================
Запуск:
  pip install requests
  python astana_hub_farmer.py

Файлы:
  history.json  — какие посты уже просмотрены/лайкнуты/прокомментированы

Точные эндпоинты из JS сайта:
  Просмотр:     POST /community/api/blog/{id}/read/
  Комментарий:  POST /api/comment/   {"message": "...", "primary_key": "{id}", "source": "Blog"}
  Лайк поста:   GET  /api/blog/{id}/reaction_up/
  Посты:        GET  /community/api/blog/?feed=true&order_by=-publish_date&page=N&page_size=20
"""

import importlib
import requests
import json
import time
import logging
import os
import re
import random
from cookie_server import start_cookie_server, load_cookies_from_file, COOKIES_FILE, is_paused

# ─────────────────────────── CONFIG ──────────────────────────────────────────

BASE_URL     = "https://astanahub.com"
REQUEST_TIMEOUT = int(os.environ.get("REQUEST_TIMEOUT", "30"))  # Увеличено с 15 для медленных соединений

# Путь к файлу истории — переопределяется через ENV в Docker
HISTORY_FILE = os.environ.get("HISTORY_FILE", "history.json")

# Куки берутся из переменных окружения (задаются в CapRover -> App -> ENV VARS)
# Можно передать полную строку куки через COOKIE_STRING
# или отдельные значения SESSION_ID / CSRF_TOKEN
# Куки загружаются динамически из файла (обновляются через веб-интерфейс)
# При первом старте берём из ENV или COOKIE_STRING как fallback
_cookie_string = os.environ.get("COOKIE_STRING", "").strip()

def _parse_cookie_string(s):
    result = {}
    for part in s.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            result[k.strip()] = v.strip()
    return result

if _cookie_string:
    _initial_cookies = _parse_cookie_string(_cookie_string)
else:
    _initial_cookies = {
        "sessionid": os.environ.get("SESSION_ID", "").strip(),
        "csrftoken":  os.environ.get("CSRF_TOKEN",  "").strip(),
    }

COOKIES = _initial_cookies  # будет обновляться динамически
_cookie_file_mtime = None
AUTO_LOAD_BROWSER_COOKIES = os.environ.get("AUTO_LOAD_BROWSER_COOKIES", "true").strip().lower() not in ("false", "0", "no", "off")

LOOP_DELAY    = int(os.environ.get("LOOP_DELAY",    "10"))
REQUEST_DELAY = int(os.environ.get("REQUEST_DELAY", "2"))
READ_DELAY    = int(os.environ.get("READ_DELAY",    "20"))

VIEW_DELAY    = int(os.environ.get("VIEW_DELAY",    "60"))
LIKE_DELAY    = int(os.environ.get("LIKE_DELAY",    "10"))
COMMENT_DELAY = int(os.environ.get("COMMENT_DELAY", "120"))

COMMENT_TEXT  = os.environ.get("COMMENT_TEXT", "👍")

# ─────────────────────────── LOGGING ─────────────────────────────────────────

logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").strip().upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

# ─────────────────────────── SESSION ─────────────────────────────────────────

session = requests.Session()
session.cookies.update(COOKIES)
session.headers.update({
    "Accept":             "application/json, text/plain, */*",
    "Accept-Language":    "ru,en;q=0.9",
    "Referer":            BASE_URL + "/community/",
    "User-Agent":         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0",
    "X-CSRFToken":        COOKIES.get("csrftoken", ""),
    "sec-fetch-dest":     "empty",
    "sec-fetch-mode":     "cors",
    "sec-fetch-site":     "same-origin",
})

def sleep_with_pause(seconds: float) -> None:
    """Sleep in short intervals and honor pause state."""
    end_time = time.time() + max(0, seconds)
    while True:
        if is_paused():
            time.sleep(0.25)
            continue
        remaining = end_time - time.time()
        if remaining <= 0:
            break
        time.sleep(min(0.25, remaining))

# ─────────────────────────── HISTORY ─────────────────────────────────────────

def load_history() -> dict:
    if not os.path.exists(HISTORY_FILE):
        return {"viewed": [], "liked": [], "commented": []}
    with open(HISTORY_FILE) as f:
        return json.load(f)


def save_history(history: dict) -> None:
    with open(HISTORY_FILE, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def mark_done(history: dict, action: str, post_id) -> None:
    pid = str(post_id)
    if pid not in history.get(action, []):
        history.setdefault(action, []).append(pid)
    save_history(history)


def already_done(history: dict, action: str, post_id) -> bool:
    return str(post_id) in history.get(action, [])

# ─────────────────────────── POSTS API ───────────────────────────────────────

def get_posts(page: int = 1, page_size: int = 20) -> list[dict]:
    """GET /community/api/blog/?feed=true&order_by=-publish_date"""
    resp = session.get(
        BASE_URL + "/community/api/blog/",
        params={
            "page":      page,
            "page_size": page_size,
            "feed":      "true",
            "order_by":  "-publish_date",
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def get_random_post(max_id: int) -> dict | None:
    """Получить случайный пост по ID от 1 до max_id"""
    for attempt in range(10):  # Максимум 10 попыток найти существующий пост
        post_id = random.randint(1, max_id)
        try:
            resp = session.get(BASE_URL + f"/community/api/blog/{post_id}/", timeout=REQUEST_TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 404:
                continue  # Пост не существует, попробуем другой
        except Exception:
            continue
    return None


def view_post(post: dict) -> bool:
    """
    1. GET  /community/api/blog/{id}/   — загрузить пост
    2. POST /community/api/blog/{id}/read/ — засчитать просмотр
    """
    pid = post["id"]
    try:
        # Шаг 1: загрузить детали поста (как браузер)
        session.get(BASE_URL + f"/community/api/blog/{pid}/", timeout=REQUEST_TIMEOUT)
        sleep_with_pause(1)
        # Шаг 2: отметить как прочитанный
        resp = session.post(BASE_URL + f"/community/api/blog/{pid}/read/", timeout=REQUEST_TIMEOUT)
        log.debug("  view_post %s -> %s %s", pid, resp.status_code, resp.text[:100])
        return resp.status_code in (200, 201)
    except Exception as e:
        log.warning("  Ошибка view_post %s: %s", pid, e)
        return False


def react_post(post: dict) -> bool:
    """
    Лайк поста. Пробуем несколько вариантов эндпоинта.
    GET /api/blog/{id}/reaction_up/
    """
    pid = post["id"]
    try:
        resp = session.get(BASE_URL + f"/api/blog/{pid}/reaction_up/", timeout=REQUEST_TIMEOUT)
        return resp.status_code == 200
    except Exception as e:
        log.warning("  Ошибка react_post %s: %s", pid, e)
        return False


def comment_post(post: dict) -> tuple[bool, str]:
    """
    POST /api/comment/
    Body: {"message": "...", "primary_key": "{id}", "source": "Blog"}
    Returns: (success, error_message)
    """
    pid = post["id"]
    try:
        # Генерируем случайное 8-значное число для комментария
        random_comment = str(random.randint(10000000, 99999999))
        resp = session.post(
            BASE_URL + "/api/comment/",
            json={
                "message":     random_comment,
                "primary_key": str(pid),
                "source":      "Blog",
            },
            timeout=REQUEST_TIMEOUT,
        )
        if resp.status_code in (200, 201):
            return True, ""
        else:
            return False, f"HTTP {resp.status_code}: {resp.text[:100]}"
    except Exception as e:
        return False, str(e)

# ─────────────────────────── QUEST HELPERS ───────────────────────────────────

def get_title(quest: dict) -> str:
    t = quest.get("title", {})
    if isinstance(t, dict):
        return t.get("ru") or t.get("en") or ""
    return str(t)


def get_required_count(quest: dict) -> int:
    nums = re.findall(r'\d+', get_title(quest))
    return int(nums[0]) if nums else 1


def classify(quest: dict) -> str:
    if quest.get("claimed"):
        return "done"
    title = get_title(quest).lower()
    EASY = {"read", "view", "watch", "open", "visit", "explore",
            "прочитай", "посмотри", "зайди", "коммент", "прокомментируй", "like", "лайк"}
    HARD = {"publish", "write", "create", "get", "collect", "invite",
            "опубликуй", "напиши", "создай", "получи", "собери"}
    if any(k in title for k in HARD):
        return "hard"
    if any(k in title for k in EASY):
        return "easy"
    return "unknown"


def detect_action(quest: dict) -> str:
    title = get_title(quest).lower()
    d     = quest.get("description", {})
    desc  = ((d.get("ru") or d.get("en") or "") if isinstance(d, dict) else str(d)).lower()
    text  = title + " " + desc
    if any(k in text for k in ("like", "лайк", "reaction")):
        return "like"
    if any(k in text for k in ("comment", "коммент")):
        return "comment"
    return "view"

# ─────────────────────────── QUEST EXECUTION ─────────────────────────────────

def get_post_title(post: dict) -> str:
    t = post.get("title", {})
    if isinstance(t, dict):
        return (t.get("ru") or t.get("en") or str(post["id"]))[:55]
    return str(t)[:55]


def execute_quest(quest: dict, history: dict) -> bool:
    action  = detect_action(quest)
    needed  = get_required_count(quest)
    hkey    = {"view": "viewed", "like": "liked", "comment": "commented"}[action]

    progress = quest.get("progress", 0)
    completed = int(progress / 100 * needed)
    log.info("  Действие: %-8s | нужно: %d | прогресс: %d%% (%d/%d)",
             action, needed, progress, completed, needed)

    # Получить максимальный ID поста
    try:
        first_posts = get_posts(page=1, page_size=1)
        if not first_posts:
            log.error("  Не удалось получить посты для определения max_id")
            return False
        max_id = first_posts[0]["id"]
        log.info("  Максимальный ID поста: %d", max_id)
    except Exception as e:
        log.error("  Ошибка при получении max_id: %s", e)
        return False

    processed_ids = set()
    failed_attempts = 0
    max_failed_attempts = 100  # Максимум неудачных попыток найти пост
    iteration = 0

    while completed < needed:
        # Выбираем случайный пост
        post = get_random_post(max_id)
        if not post:
            failed_attempts += 1
            if failed_attempts >= max_failed_attempts:
                log.warning("  Превышено максимум неудачных попыток (%d), прекращаем", max_failed_attempts)
                break
            log.debug("  Не удалось найти пост, попытка %d/%d", failed_attempts, max_failed_attempts)
            sleep_with_pause(REQUEST_DELAY)
            continue

        pid = str(post["id"])

        # Пропускаем уже обработанные в этой сессии
        if pid in processed_ids:
            continue
        processed_ids.add(pid)

        # Пропускаем уже обработанные в истории
        if already_done(history, hkey, pid):
            continue

        # Для лайка — пропускаем уже лайкнутые по данным API
        if action == "like" and post.get("reacted_up"):
            mark_done(history, hkey, pid)
            continue

        try:
            # Для просмотра — имитируем 20 сек чтения перед mark_read
            if action == "view":
                log.info("  👁  [%d/%d] Читаю '%s'...",
                         completed + 1, needed, get_post_title(post))
                success = view_post(post)
                log.info("  👁  Просмотр — %s", "✓" if success else "✗")

            elif action == "like":
                success = react_post(post)
                log.info("  ❤️  [%d/%d] Лайк '%s' — %s",
                         completed + 1, needed, get_post_title(post),
                         "✓" if success else "✗")

            elif action == "comment":
                success, error_msg = comment_post(post)
                log.info("  💬 [%d/%d] Комментарий '%s' — %s",
                         completed + 1, needed, get_post_title(post),
                         "✓" if success else "✗")
                if not success:
                    log.info("    Причина: %s", error_msg)
                    if "Превышено допустимое количество комментариев" in error_msg:
                        log.warning("  Лимит комментариев превышен, удаляем квест")
                        try:
                            remove_quest(quest["id"])
                            log.info("  Квест удален")
                        except Exception as e:
                            log.error("  Ошибка удаления квеста: %s", e)
                        return False

            if success:
                mark_done(history, hkey, pid)
                completed += 1
                iteration += 1

                # Каждые 2 итерации проверяем обновление прогресса на сервере
                if iteration % 2 == 0:
                    try:
                        current_quests = get_quests()
                        for q in current_quests:
                            if q["id"] == quest["id"]:
                                quest.update(q)
                                new_progress = quest.get("progress", 0)
                                new_completed = int(new_progress / 100 * needed)
                                if new_completed > completed:
                                    log.info("  📊 Обновлен прогресс с сервера: %d%% (%d/%d)",
                                             new_progress, new_completed, needed)
                                    completed = max(completed, new_completed)
                                break
                    except Exception as e:
                        log.debug("  Ошибка проверки прогресса: %s", e)

        except Exception as e:
            log.warning("  Ошибка при обработке поста %s: %s", pid, e)
            continue  # Пропускаем и идем к следующему

        # Задержка между действиями, но не после последнего выполненного действия
        if completed < needed:
            if action == "view":
                sleep_with_pause(VIEW_DELAY)
            elif action == "like":
                sleep_with_pause(LIKE_DELAY)
            elif action == "comment":
                sleep_with_pause(COMMENT_DELAY)

    log.info("  Итог: %d/%d", completed, needed)
    return completed >= needed

# ─────────────────────────── QUESTS API ──────────────────────────────────────

def _get_cookies_file_mtime() -> float | None:
    try:
        return os.path.getmtime(COOKIES_FILE)
    except OSError:
        return None


def load_browser_cookies(domain_name: str = "astanahub.com") -> dict:
    """Load cookies for a domain from the local logged-in browser session."""
    log.info("🔍 Попытка загрузить куки из браузера для домена: %s", domain_name)
    
    try:
        browser_cookie3 = importlib.import_module("browser_cookie3")
    except ModuleNotFoundError:
        log.warning("❌ browser_cookie3 не установлен. Установи: pip install browser-cookie3")
        return {}

    loader_names = [
        "chrome",      # Chrome обычно не блокирует доступ к куки
        "brave",       # Brave использует Chrome профиль
        "edge",        # Edge может быть заблокирован если запущен
        "firefox",
        "opera",
    ]
    loaders = [getattr(browser_cookie3, name, None) for name in loader_names]
    loaders = [loader for loader in loaders if callable(loader)]
    
    log.info("🌐 Доступные загрузчики: %s", [l.__name__ for l in loaders])

    for loader in loaders:
        loader_name = loader.__name__
        log.debug("  → Попытка загрузить куки из %s...", loader_name)
        try:
            jar = loader(domain_name=domain_name)
            log.debug("    ✓ Открыта jar сессия из %s", loader_name)
        except PermissionError as exc:
            log.debug("    ⚠ %s заблокирован (браузер запущен?): %s", loader_name, exc)
            continue
        except Exception as exc:
            log.warning("    ✗ %s ошибка: %s", loader_name, type(exc).__name__ + ": " + str(exc))
            continue

        cookies = {}
        for c in jar:
            domain = (c.domain or "").lstrip('.')
            if domain_name in domain:
                cookies[c.name] = c.value
        
        if cookies:
            log.info("✅ Загружены куки из браузера: %s. Найдено куки: %s", loader_name, sorted(cookies.keys()))
            return cookies
        else:
            log.debug("    Куки для %s не найдены в %s", domain_name, loader_name)

    log.warning("❌ Куки из браузера не получены. Проверь:")
    log.warning("   1. Авторизован ли ты в браузере на astanahub.com")
    log.warning("   2. Запущен ли браузер (Chrome, Edge, Firefox, Opera или Brave)")
    log.warning("   3. Не заблокирована ли папка профиля браузера")
    return {}


def refresh_cookies() -> None:
    """Загружает куки из браузера или файла, полностью заменяет старые."""
    global _cookie_file_mtime

    if AUTO_LOAD_BROWSER_COOKIES:
        log.info("--- Обновление куки ---")
        browser_cookies = load_browser_cookies()
        if browser_cookies:
            session.cookies.clear()
            for k, v in browser_cookies.items():
                session.cookies.set(k, v, domain="astanahub.com")
            csrf = browser_cookies.get("csrftoken", "")
            if csrf:
                session.headers.update({"X-CSRFToken": csrf})
                log.info("✓ Установлен X-CSRFToken из браузера")
            _cookie_file_mtime = _get_cookies_file_mtime()
            log.info("✓ Куки из браузера активированы")
            return
        else:
            log.warning("⚠ Куки из браузера недоступны, пробуем cookies.json...")

    file_cookies = load_cookies_from_file()
    if not file_cookies:
        log.error("❌ Куки не загружены ни из браузера, ни из cookies.json!")
        return

    log.info("✓ Загружены куки из файла: %s", COOKIES_FILE)
    session.cookies.clear()
    for k, v in file_cookies.items():
        session.cookies.set(k, v, domain="astanahub.com")

    csrf = file_cookies.get("csrftoken", "")
    if csrf:
        session.headers.update({"X-CSRFToken": csrf})
        log.info("✓ Установлен X-CSRFToken из файла")

    _cookie_file_mtime = _get_cookies_file_mtime()
    log.info("✓ Куки из файла активированы")


def refresh_cookies_if_needed() -> None:
    global _cookie_file_mtime
    current_mtime = _get_cookies_file_mtime()
    if current_mtime and current_mtime != _cookie_file_mtime:
        log.info("🔄 Куки обновлены на диске. Перезагружаем...")
        refresh_cookies()


def refresh_csrf() -> None:
    refresh_cookies_if_needed()


def get_quests() -> list[dict]:
    refresh_csrf()
    resp = session.get(
        BASE_URL + "/s/games/api/quests/",
        params={"module": "blog"},
        timeout=REQUEST_TIMEOUT,
    )
    if resp.status_code != 200:
        log.error("quests -> %s | body: %s | cookies sent: %s",
                  resp.status_code,
                  resp.text[:200],
                  [c.name for c in session.cookies])
    resp.raise_for_status()
    return resp.json().get("active_quests", [])


def claim_reward(quest_id) -> dict:
    resp = session.get(
        BASE_URL + f"/s/games/api/quests/{quest_id}/claim/", timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def remove_quest(quest_id) -> bool:
    resp = session.post(
        BASE_URL + "/s/games/api/quests/remove/",
        json={"quest": quest_id},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return bool(resp.json())

# ─────────────────────────── MAIN LOOP ───────────────────────────────────────

def run_farmer() -> None:
    # Запускаем cookie-сервер в фоне
    start_cookie_server()

    log.info("🚀 Фармер запущен. Нажми Ctrl+C для остановки.")
    log.info("📂 История: %s", HISTORY_FILE)
    log.info("🌐 Веб-интерфейс для обновления куки: http://localhost:8765\n")

    # Загружаем куки из файла если есть
    refresh_cookies()

    # Проверка доступности сервера (с повторными попытками каждые 5 минут)
    server_available = False
    while not server_available:
        log.info("🔗 Проверка доступности сервера %s...", BASE_URL)
        try:
            resp = session.head(BASE_URL, timeout=10)
            log.info("✓ Сервер доступен (%s)", resp.status_code)
            server_available = True
        except requests.ConnectionError as e:
            log.error("❌ Сервер недоступен! Ошибка: %s", e)
            log.error("   Возможные причины:")
            log.error("   1. Нет интернета")
            log.error("   2. Сервер astanahub.com недоступен")
            log.error("   3. Блокировка по IP или VPN требуется")
            log.error("   4. Проблемы с DNS")
            log.warning("⏳ Повтор через 5 минут...")
            sleep_with_pause(300)  # 5 минут
        except requests.Timeout:
            log.error("❌ Таймаут при подключении к серверу")
            log.warning("⏳ Повтор через 5 минут...")
            sleep_with_pause(300)  # 5 минут

    history        = load_history()
    total          = 0
    unknown_streak = 0
    MAX_UNKNOWN    = 10
    connection_errors = 0
    MAX_CONNECTION_ERRORS = 5

    while True:
        # Пауза
        if is_paused():
            sleep_with_pause(2)
            continue

        try:
            quests = get_quests()
            connection_errors = 0  # Сброс счётчика при успешном подключении
            log.info("Активных квестов: %d", len(quests))

            for q in quests:
                kind = classify(q)
                prog = q.get("progress", 0)
                log.info("  [%-7s] id=%-4s  %3s%%  %s",
                         kind.upper(), q["id"], prog, get_title(q))

            # 1. Удаляем уже claimed
            for quest in quests:
                if classify(quest) == "done":
                    qid = quest["id"]
                    log.info("▶ Удаляю выполненный id=%s '%s'", qid, get_title(quest))
                    sleep_with_pause(REQUEST_DELAY)
                    if remove_quest(qid):
                        log.info("  🗑 Удалён -> появится новый")

            # 2. Удаляем непонятные квесты по одному за итерацию
            unknown_quest = next(
                (q for q in quests
                 if classify(q) == "unknown" and not q.get("claimed")),
                None
            )
            if unknown_quest is not None:
                if unknown_streak >= MAX_UNKNOWN:
                    log.error(
                        "СТОП: удалено %d непонятных квестов подряд!\n"
                        "  Зайди на сайт, проверь квесты вручную и перезапусти скрипт.",
                        MAX_UNKNOWN
                    )
                    break
                qid = unknown_quest["id"]
                log.warning("НЕИЗВЕСТНЫЙ квест id=%s '%s' — удаляю (%d/%d)",
                            qid, get_title(unknown_quest),
                            unknown_streak + 1, MAX_UNKNOWN)
                sleep_with_pause(REQUEST_DELAY)
                if remove_quest(qid):
                    unknown_streak += 1
                    log.info("  Удалён (подряд неизвестных: %d/%d)",
                             unknown_streak, MAX_UNKNOWN)
                sleep_with_pause(LOOP_DELAY)
                continue

            # Если появился лёгкий — сбрасываем счётчик неизвестных
            # 3. Берём первый лёгкий
            easy = next(
                (q for q in quests if classify(q) == "easy" and not q.get("claimed")),
                None
            )

            if easy is None:
                log.info("Лёгких квестов нет. Пауза %d сек...\n", LOOP_DELAY * 5)
                sleep_with_pause(LOOP_DELAY * 5)
                continue

            unknown_streak = 0  # сбрасываем — нашли нормальный квест

            qid = easy["id"]
            log.info("\n▶ Выполняю: id=%s '%s'  прогресс=%s%%",
                     qid, get_title(easy), easy.get("progress", 0))

            # 2a. Выполнить на постах
            if not easy.get("completed"):
                success = execute_quest(easy, history)
                if not success:
                    log.warning("  Не удалось выполнить полностью, пробуем позже\n")
                    sleep_with_pause(LOOP_DELAY)
                    continue
                sleep_with_pause(REQUEST_DELAY)

            # 2b. Забрать награду
            log.info("  → Забираю награду...")
            reward_data = claim_reward(qid)
            coins = (reward_data.get("social_coins")
                     or reward_data.get("coins")
                     or easy.get("social_coins", "?"))
            xp    = (reward_data.get("experience_points")
                     or easy.get("experience_points", "?"))
            log.info("  💰 Получено: %s коинов, %s XP", coins, xp)
            sleep_with_pause(REQUEST_DELAY)

            # 2c. Удалить квест
            if remove_quest(qid):
                log.info("  🗑 Квест id=%s удалён → появится новый\n", qid)

            total += 1
            log.info("✅ Итого выполнено: %d\n", total)
            sleep_with_pause(LOOP_DELAY)

        except requests.HTTPError as e:
            code = e.response.status_code if e.response is not None else 0
            if code in (401, 403):
                log.error(
                    "HTTP %s — сессия истекла!\n"
                    "  1. Зайди на astanahub.com\n"
                    "  2. DevTools → Network → quests → Request Headers\n"
                    "  3. Скопируй sessionid и csrftoken в COOKIES\n"
                    "  4. Перезапусти скрипт", code
                )
                break
            elif code == 429:
                log.warning("429 — слишком много запросов. Ждём 60 сек...")
                sleep_with_pause(60)
            else:
                log.error("HTTP %s: %s", code, e)
                sleep_with_pause(LOOP_DELAY * 2)

        except (requests.ConnectionError, ConnectionError) as e:
            log.error("❌ Ошибка соединения: %s", e)
            
            # Проверяем, это ошибка типа RemoteDisconnected или подобная
            error_str = str(e).lower()
            if any(err in error_str for err in ["remote end closed", "remoteDisconnected", "connection aborted"]):
                log.warning("⚠ Сервер закрыл соединение. Ожидаем восстановления...")
                connection_errors = 0  # Сбрасываем счетчик для новой попытки
                sleep_with_pause(300)  # 5 минут
                continue  # Возвращаемся в начало цикла
            
            connection_errors += 1
            log.error("❌ Ошибка соединения (%d/%d)", connection_errors, MAX_CONNECTION_ERRORS)
            
            if connection_errors >= MAX_CONNECTION_ERRORS:
                log.error("💥 Слишком много ошибок соединения! Ожидаем восстановления...")
                connection_errors = 0
                sleep_with_pause(300)  # 5 минут
                continue
            
            wait_time = 30 * connection_errors  # Увеличивается с каждой ошибкой: 30, 60, 90, 120, 150
            log.warning("⏳ Ожидание %d сек перед повтором...", wait_time)
            sleep_with_pause(wait_time)

        except KeyboardInterrupt:
            log.info("\n⏹ Остановлено. Выполнено квестов: %d", total)
            break

        except Exception as e:
            log.exception("Неожиданная ошибка: %s", e)
            sleep_with_pause(LOOP_DELAY * 2)


if __name__ == "__main__":
    run_farmer()
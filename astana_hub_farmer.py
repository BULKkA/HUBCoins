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

import requests
import json
import time
import logging
import os
import re
from cookie_server import start_cookie_server, load_cookies_from_file, COOKIES_FILE, is_paused

# ─────────────────────────── CONFIG ──────────────────────────────────────────

BASE_URL     = "https://astanahub.com"

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

LOOP_DELAY    = int(os.environ.get("LOOP_DELAY",    "10"))
REQUEST_DELAY = int(os.environ.get("REQUEST_DELAY", "2"))
READ_DELAY    = int(os.environ.get("READ_DELAY",    "5"))

COMMENT_TEXT  = os.environ.get("COMMENT_TEXT", "Интересный пост, спасибо за информацию! 👍")

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
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json().get("results", [])


def view_post(post: dict) -> bool:
    """
    1. GET  /community/api/blog/{id}/   — загрузить пост
    2. POST /community/api/blog/{id}/read/ — засчитать просмотр
    """
    pid = post["id"]
    try:
        # Шаг 1: загрузить детали поста (как браузер)
        session.get(BASE_URL + f"/community/api/blog/{pid}/", timeout=15)
        time.sleep(1)
        # Шаг 2: отметить как прочитанный
        resp = session.post(BASE_URL + f"/community/api/blog/{pid}/read/", timeout=15)
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
        resp = session.get(BASE_URL + f"/api/blog/{pid}/reaction_up/", timeout=15)
        return resp.status_code == 200
    except Exception as e:
        log.warning("  Ошибка react_post %s: %s", pid, e)
        return False


def comment_post(post: dict) -> bool:
    """
    POST /api/comment/
    Body: {"message": "...", "primary_key": "{id}", "source": "Blog"}
    """
    pid = post["id"]
    try:
        resp = session.post(
            BASE_URL + "/api/comment/",
            json={
                "message":     COMMENT_TEXT,
                "primary_key": str(pid),
                "source":      "Blog",
            },
            timeout=15,
        )
        return resp.status_code in (200, 201)
    except Exception as e:
        log.warning("  Ошибка comment_post %s: %s", pid, e)
        return False

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
            "прочитай", "посмотри", "зайди"}
    HARD = {"publish", "write", "create", "get", "collect", "invite",
            "like", "опубликуй", "напиши", "создай", "получи", "собери"}
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

    log.info("  Действие: %-8s | нужно: %d | уже в истории: %d",
             action, needed, len(history.get(hkey, [])))

    completed = 0
    page      = 1

    while completed < needed:
        posts = get_posts(page=page, page_size=20)
        if not posts:
            log.warning("  Страница %d пуста", page)
            break

        log.info("  Страница %d — %d постов", page, len(posts))

        for post in posts:
            if completed >= needed:
                break

            pid = str(post["id"])

            # Пропускаем уже обработанные
            if already_done(history, hkey, pid):
                continue

            # Для лайка — пропускаем уже лайкнутые по данным API
            if action == "like" and post.get("reacted_up"):
                mark_done(history, hkey, pid)
                continue

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
                success = comment_post(post)
                log.info("  💬 [%d/%d] Комментарий '%s' — %s",
                         completed + 1, needed, get_post_title(post),
                         "✓" if success else "✗")

            if success:
                mark_done(history, hkey, pid)
                completed += 1

            time.sleep(READ_DELAY)

        page += 1
        if page > 20:
            log.warning("  Достигнут лимит страниц (20)")
            break

    log.info("  Итог: %d/%d", completed, needed)
    return completed >= needed

# ─────────────────────────── QUESTS API ──────────────────────────────────────

def refresh_cookies() -> None:
    """
    Загружает актуальные куки из файла и применяет их к сессии.
    Полностью заменяет старые куки чтобы не было дублей.
    """
    file_cookies = load_cookies_from_file()
    if file_cookies:
        session.cookies.clear()
        session.cookies.update(file_cookies)

    # Обновляем X-CSRFToken из актуальных куки
    csrf = session.cookies.get("csrftoken", "")
    if not csrf:
        # fallback — берём первый подходящий
        for c in session.cookies:
            if c.name == "csrftoken":
                csrf = c.value
                break
    if csrf:
        session.headers.update({"X-CSRFToken": csrf})


def refresh_csrf() -> None:
    refresh_cookies()


def get_quests() -> list[dict]:
    refresh_csrf()
    resp = session.get(BASE_URL + "/s/games/api/quests/", timeout=15)
    if resp.status_code != 200:
        log.error("quests -> %s | cookies: %s",
                  resp.status_code,
                  {c.name: c.value[:8]+"..." for c in session.cookies})
    resp.raise_for_status()
    return resp.json().get("active_quests", [])


def claim_reward(quest_id) -> dict:
    resp = session.get(
        BASE_URL + f"/s/games/api/quests/{quest_id}/claim/", timeout=15)
    resp.raise_for_status()
    return resp.json()


def remove_quest(quest_id) -> bool:
    resp = session.post(
        BASE_URL + "/s/games/api/quests/remove/",
        json={"quest": quest_id},
        timeout=15,
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

    history        = load_history()
    total          = 0
    unknown_streak = 0   # сколько непонятных квестов удалено подряд
    MAX_UNKNOWN    = 10  # лимит — после него остановка до ручного запуска

    while True:
        # Пауза
        if is_paused():
            time.sleep(2)
            continue

        try:
            quests = get_quests()
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
                    time.sleep(REQUEST_DELAY)
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
                time.sleep(REQUEST_DELAY)
                if remove_quest(qid):
                    unknown_streak += 1
                    log.info("  Удалён (подряд неизвестных: %d/%d)",
                             unknown_streak, MAX_UNKNOWN)
                time.sleep(LOOP_DELAY)
                continue

            # Если появился лёгкий — сбрасываем счётчик неизвестных
            # 3. Берём первый лёгкий
            easy = next(
                (q for q in quests if classify(q) == "easy" and not q.get("claimed")),
                None
            )

            if easy is None:
                log.info("Лёгких квестов нет. Пауза %d сек...\n", LOOP_DELAY * 5)
                time.sleep(LOOP_DELAY * 5)
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
                    time.sleep(LOOP_DELAY)
                    continue
                time.sleep(REQUEST_DELAY)

            # 2b. Забрать награду
            log.info("  → Забираю награду...")
            reward_data = claim_reward(qid)
            coins = (reward_data.get("social_coins")
                     or reward_data.get("coins")
                     or easy.get("social_coins", "?"))
            xp    = (reward_data.get("experience_points")
                     or easy.get("experience_points", "?"))
            log.info("  💰 Получено: %s коинов, %s XP", coins, xp)
            time.sleep(REQUEST_DELAY)

            # 2c. Удалить квест
            if remove_quest(qid):
                log.info("  🗑 Квест id=%s удалён → появится новый\n", qid)

            total += 1
            log.info("✅ Итого выполнено: %d\n", total)
            time.sleep(LOOP_DELAY)

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
                time.sleep(60)
            else:
                log.error("HTTP %s: %s", code, e)
                time.sleep(LOOP_DELAY * 2)

        except requests.ConnectionError:
            log.error("Нет соединения. Повтор через 30 сек...")
            time.sleep(30)

        except KeyboardInterrupt:
            log.info("\n⏹ Остановлено. Выполнено квестов: %d", total)
            break

        except Exception as e:
            log.exception("Неожиданная ошибка: %s", e)
            time.sleep(LOOP_DELAY * 2)


if __name__ == "__main__":
    run_farmer()
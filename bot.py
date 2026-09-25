"""Telegram-бот: фото (+ заметки) -> готовый пост в стиле канала.

Работает короткими запусками в GitHub Actions: забирает накопившиеся
сообщения через getUpdates, отвечает и завершается. Только стандартная
библиотека Python, ничего устанавливать не нужно.

Переменные окружения:
  TELEGRAM_TOKEN     - токен бота от @BotFather (секрет)
  ANTHROPIC_API_KEY  - ключ Claude API (секрет)
  ALLOWED_USER_IDS   - ID пользователей через запятую, кому можно пользоваться ботом
  CLAUDE_MODEL       - (необязательно) модель; по умолчанию берётся новейшая Sonnet
  POLL_SECONDS       - (необязательно) сколько секунд ждать новые сообщения, по умолчанию 40
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent

# настройки из config.txt (строки вида КЛЮЧ=значение), если файл есть
_cfg = HERE / "config.txt"
if _cfg.exists():
    for _line in _cfg.read_text(encoding="utf-8-sig").splitlines():
        if "=" in _line and not _line.strip().startswith("#"):
            _k, _v = _line.split("=", 1)
            if _v.strip():
                os.environ.setdefault(_k.strip(), _v.strip())

TG_TOKEN = os.environ["TELEGRAM_TOKEN"].strip()
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
ALLOWED = {s.strip() for s in os.environ.get("ALLOWED_USER_IDS", "").split(",") if s.strip()}
POLL_SECONDS = int(os.environ.get("POLL_SECONDS") or 0)  # 0 = работать постоянно
OWNER_FILE = HERE / "owner.txt"
if not ALLOWED and OWNER_FILE.exists():
    ALLOWED = {s.strip() for s in OWNER_FILE.read_text().split(",") if s.strip()}
TG = f"https://api.telegram.org/bot{TG_TOKEN}"
MAX_IMAGES = 5


# ---------- HTTP ----------
def http(url, data=None, headers=None, timeout=120):
    body = json.dumps(data).encode() if data is not None else None
    h = {"Content-Type": "application/json"} if body else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:500]}") from None


def tg(method, **params):
    res = http(f"{TG}/{method}", params, timeout=params.get("timeout", 0) + 30)
    if not res.get("ok"):
        raise RuntimeError(f"Telegram {method}: {res}")
    return res["result"]


def download_photo(file_id):
    path = tg("getFile", file_id=file_id)["file_path"]
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}", timeout=60) as r:
        return base64.b64encode(r.read()).decode()


# ---------- Claude ----------
_model = None


def model():
    global _model
    if _model:
        return _model
    _model = os.environ.get("CLAUDE_MODEL", "").strip()
    if not _model:
        try:
            models = http("https://api.anthropic.com/v1/models?limit=100", headers=claude_headers())["data"]
            _model = next(m["id"] for m in models if "sonnet" in m["id"])
        except Exception as e:
            print("Не удалось получить список моделей:", e)
            _model = "claude-sonnet-4-5"
    print("Модель:", _model)
    return _model


def claude_headers():
    return {"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"}


SYSTEM = [
    {"type": "text", "text": (HERE / "style.md").read_text(encoding="utf-8")},
    {
        "type": "text",
        "text": "Примеры настоящих постов канала по рубрикам (в Telegram HTML). Пиши так же:\n\n"
        + (HERE / "examples.txt").read_text(encoding="utf-8")
        + "\n\nОтвечай ТОЛЬКО текстом готового поста в Telegram HTML (и при необходимости строкой NOTE), без пояснений и без тегов <post>.",
        "cache_control": {"type": "ephemeral"},
    },
]


def ask_claude(content):
    res = http(
        "https://api.anthropic.com/v1/messages",
        {"model": model(), "max_tokens": 2000, "system": SYSTEM, "messages": [{"role": "user", "content": content}]},
        headers=claude_headers(),
        timeout=180,
    )
    return "".join(b.get("text", "") for b in res["content"]).strip()


# ---------- Логика ----------
HELP = (
    "Пришлите фото (или несколько одним альбомом) и в подписи напишите заметки: "
    "бренд, название, впечатления, цену, адрес — всё, что важно. В ответ придёт готовый пост.\n\n"
    "Чтобы поправить пост — ответьте (reply) на него с пожеланием, например: «короче», «добавь вопрос в конце».\n"
    "Без фото тоже можно: просто пришлите заметки текстом."
)


def send(chat_id, text, reply_to=None):
    for i in range(0, len(text), 4000):
        chunk = text[i : i + 4000]
        params = {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        try:
            tg("sendMessage", **params)
        except RuntimeError:  # сломанная разметка — отправим как простой текст
            params.pop("parse_mode")
            tg("sendMessage", **params)


def handle(group):
    """group - список сообщений (альбом = несколько сообщений с общим media_group_id)."""
    first = group[0]
    chat_id = first["chat"]["id"]
    user_id = str(first.get("from", {}).get("id", ""))
    notes = "\n".join(m.get("caption") or m.get("text") or "" for m in group).strip()

    if not ALLOWED and user_id and not os.environ.get("GITHUB_ACTIONS"):  # первый, кто написал боту, становится владельцем
        ALLOWED.add(user_id)
        OWNER_FILE.write_text(user_id)
        send(chat_id, "Вы назначены владельцем бота ✅")
    if user_id not in ALLOWED:
        send(chat_id, f"Доступ закрыт. Ваш ID: <code>{user_id}</code>\nВладелец может добавить его в config.txt (ALLOWED_USER_IDS).")
        return
    if notes.startswith("/start") or notes.startswith("/help"):
        send(chat_id, HELP)
        return

    reply = first.get("reply_to_message")
    if reply and reply.get("from", {}).get("is_bot") and not any("photo" in m for m in group):
        content = [{"type": "text", "text": f"Вот пост:\n\n{reply.get('text', '')}\n\nПерепиши его в стиле канала с учётом пожелания автора: {notes}\n\nЕсли автор дописала факты — используй их."}]
    else:
        photos = [m["photo"][-1]["file_id"] for m in group if "photo" in m][:MAX_IMAGES]
        photos += [m["document"]["file_id"] for m in group if m.get("document", {}).get("mime_type", "").startswith("image/")][: MAX_IMAGES - len(photos)]
        if not photos and not notes:
            send(chat_id, HELP)
            return
        content = []
        for fid in photos:
            content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": download_photo(fid)}})
        content.append({"type": "text", "text": "Напиши пост для канала по этим фото." + (f"\n\nЗаметки автора:\n{notes}" if notes else "\n\nЗаметок нет — опирайся на фото.")})

    tg("sendChatAction", chat_id=chat_id, action="typing")
    try:
        post = ask_claude(content)
    except Exception as e:
        send(chat_id, f"Не получилось сгенерировать пост: {str(e)[:300]}")
        raise
    note = ""
    if "NOTE:" in post:
        post, note = post.split("NOTE:", 1)
        post, note = post.strip(), note.strip()
    post = post.replace("<post>", "").replace("</post>", "").strip()
    send(chat_id, post, reply_to=first["message_id"])
    if note:
        send(chat_id, "💡 " + note)


def process(updates):
    if any(u.get("message", {}).get("media_group_id") for u in updates):
        time.sleep(2)  # даём альбому догрузиться
        updates += tg("getUpdates", offset=updates[-1]["update_id"] + 1, timeout=0, allowed_updates=["message"])
    groups, order = {}, []  # собираем альбомы вместе
    for u in updates:
        m = u.get("message")
        if not m:
            continue
        key = m.get("media_group_id") or f"single{u['update_id']}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(m)
    offset = updates[-1]["update_id"] + 1
    tg("getUpdates", offset=offset, timeout=0)  # подтверждаем, чтобы не обработать дважды
    for key in order:
        try:
            handle(groups[key])
            log(f"Обработано сообщение от {groups[key][0].get('from', {}).get('id')}")
        except Exception as e:
            log(f"Ошибка: {e}")
    return offset


def log(msg):
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + msg
    print(line, flush=True)
    try:
        with open(HERE / "bot.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def main():
    import socket
    lock = socket.socket()
    try:  # защита от двойного запуска
        lock.bind(("127.0.0.1", 47651))
    except OSError:
        print("Бот уже запущен.")
        return
    if not ANTHROPIC_KEY:
        log("ВНИМАНИЕ: не указан ANTHROPIC_API_KEY в config.txt")
    (HERE / "bot.pid").write_text(str(os.getpid()))
    log("Бот запущен, жду сообщения...")
    deadline = time.time() + POLL_SECONDS if POLL_SECONDS else None
    offset = None
    while deadline is None or time.time() < deadline:
        try:
            wait = 50 if deadline is None else max(0, min(25, int(deadline - time.time())))
            updates = tg("getUpdates", offset=offset, timeout=wait, allowed_updates=["message"])
            if updates:
                offset = process(updates)
        except Exception as e:  # нет интернета и т.п. — ждём и пробуем снова
            log(f"Сетевая ошибка: {e}")
            time.sleep(10)


if __name__ == "__main__":
    main()

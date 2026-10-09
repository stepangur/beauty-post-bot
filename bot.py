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
  VARIANTS_COUNT     - (необязательно) сколько вариантов поста предлагать, по умолчанию 2
  GITHUB_TOKEN       - (необязательно) токен с правом contents:write — чтобы команда
                        «обновить стиль» могла сама закоммитить style.md/examples.txt
  GITHUB_REPOSITORY  - (обычно задаётся GitHub Actions автоматически) "owner/repo"
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
VARIANTS = max(1, int(os.environ.get("VARIANTS_COUNT") or 2))
OWNER_FILE = HERE / "owner.txt"
if not ALLOWED and OWNER_FILE.exists():
    ALLOWED = {s.strip() for s in OWNER_FILE.read_text().split(",") if s.strip()}
TG = f"https://api.telegram.org/bot{TG_TOKEN}"
MAX_IMAGES = 5
VARIANT_SEP = "===ВАРИАНТ==="


# ---------- HTTP ----------
def http(url, data=None, headers=None, timeout=120, method=None):
    body = json.dumps(data).encode() if data is not None else None
    h = {"Content-Type": "application/json"} if body else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method=method or ("POST" if body else "GET"))
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


def download_file_bytes(file_id):
    path = tg("getFile", file_id=file_id)["file_path"]
    with urllib.request.urlopen(f"https://api.telegram.org/file/bot{TG_TOKEN}/{path}", timeout=60) as r:
        return r.read()


def download_photo(file_id):
    return base64.b64encode(download_file_bytes(file_id)).decode()


# ---------- GitHub (для команды «обновить стиль») ----------
def github_commit_file(path, content, message):
    """Коммитит файл в репозиторий бота через Contents API. Возвращает True при успехе."""
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not repo or not token:
        return False
    url = f"https://api.github.com/repos/{repo}/contents/{path}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    sha = None
    try:
        sha = http(url, headers=headers, timeout=30).get("sha")
    except RuntimeError:
        pass  # файла ещё нет — создадим новый
    body = {"message": message, "content": base64.b64encode(content.encode()).decode()}
    if sha:
        body["sha"] = sha
    http(url, body, headers=headers, method="PUT", timeout=60)
    return True


# ---------- Разбор экспорта чата Telegram (для команды «обновить стиль») ----------
_ENTITY_TAGS = {"bold": "b", "italic": "i", "underline": "u", "strikethrough": "s", "code": "code"}


def _entities_to_html(entities):
    out = []
    for e in entities:
        if isinstance(e, str):
            out.append(e)
            continue
        t, txt = e.get("type"), e.get("text", "")
        if t == "blockquote":
            out.append(f"<blockquote>{txt}</blockquote>")
        elif t in _ENTITY_TAGS:
            out.append(f"<{_ENTITY_TAGS[t]}>{txt}</{_ENTITY_TAGS[t]}>")
        else:
            out.append(txt)
    return "".join(out)


def extract_posts_from_export(raw, limit=25):
    """Достаёт тексты постов (в телеграм HTML) из result.json — экспорта истории чата."""
    data = json.loads(raw.decode("utf-8-sig"))
    msgs = data.get("messages") if isinstance(data, dict) else data
    posts = []
    for m in msgs or []:
        if m.get("type") != "message":
            continue
        ents = m.get("text_entities")
        html = _entities_to_html(ents) if ents else (m.get("text") or "")
        if not isinstance(html, str):
            html = _entities_to_html(html)
        html = html.strip()
        if len(html) >= 80:  # пропускаем короткие служебные/медийные сообщения без текста
            posts.append((m.get("date", ""), html))
    posts.sort(key=lambda p: p[0])
    return [h for _, h in posts[-limit:]]


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


def _style_md():
    return (HERE / "style.md").read_text(encoding="utf-8")


def _examples_txt():
    return (HERE / "examples.txt").read_text(encoding="utf-8")


def system_prompt():
    return [
        {"type": "text", "text": _style_md()},
        {
            "type": "text",
            "text": "Примеры настоящих постов канала по рубрикам (в Telegram HTML). Пиши так же:\n\n"
            + _examples_txt()
            + "\n\nОтвечай ТОЛЬКО текстом готового поста в Telegram HTML (и при необходимости строкой NOTE), без пояснений и без тегов <post>.",
            "cache_control": {"type": "ephemeral"},
        },
    ]


WEB_SEARCH = {"type": "web_search_20250305", "name": "web_search", "max_uses": 6}


def ask_claude(content, search=True):
    """Запрос к Claude. С search=True модель сначала ищет в интернете реальную информацию."""
    messages = [{"role": "user", "content": content}]
    body = {"model": model(), "max_tokens": 4000, "system": system_prompt(), "messages": messages}
    if search:
        body["tools"] = [WEB_SEARCH]
    for _ in range(4):  # pause_turn: модель просит продолжить долгий поиск
        try:
            res = http("https://api.anthropic.com/v1/messages", body, headers=claude_headers(), timeout=300)
        except RuntimeError as e:
            if search and "web_search" in str(e):  # поиск недоступен — пишем без него
                log(f"Поиск недоступен: {e}")
                body.pop("tools", None)
                search = False
                continue
            raise
        if res.get("stop_reason") == "pause_turn":
            messages.append({"role": "assistant", "content": res["content"]})
            continue
        break
    blocks = res["content"]
    # берём только текст после последнего результата поиска (без «сейчас поищу…»)
    last = max([i for i, b in enumerate(blocks) if b.get("type") == "web_search_tool_result"], default=-1)
    return "".join(b.get("text", "") for b in blocks[last + 1 :] if b.get("type") == "text").strip()


# ---------- Логика ----------
HELP = (
    "Пришлите фото (или несколько одним альбомом) и в подписи напишите заметки: "
    "бренд, название, впечатления, цену, адрес — всё, что важно. В ответ придут "
    + (f"{VARIANTS} варианта готового поста." if VARIANTS > 1 else "готовый пост.") + "\n\n"
    "Чтобы поправить пост — ответьте (reply) на него с пожеланием, например: «короче», «добавь вопрос в конце».\n"
    "Без фото тоже можно: просто пришлите заметки текстом.\n\n"
    "Чтобы обновить стиль по новому экспорту канала — пришлите файл result.json "
    "(Telegram Desktop → ⋮ у канала → Экспорт истории чата → JSON)."
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


def drop_status(chat_id, status_id):
    """Удаляет сообщение «идёт обработка», когда пост готов."""
    if status_id:
        try:
            tg("deleteMessage", chat_id=chat_id, message_id=status_id)
        except Exception:
            pass


def do_update_style(chat_id, doc, reply_to):
    status_id = None
    try:
        status_id = tg("sendMessage", chat_id=chat_id, text="📚 Разбираю экспорт канала и обновляю стиль, это займёт пару минут…",
                        reply_parameters={"message_id": reply_to, "allow_sending_without_reply": True})["message_id"]
    except Exception:
        pass
    try:
        posts = extract_posts_from_export(download_file_bytes(doc["file_id"]))
        if not posts:
            drop_status(chat_id, status_id)
            send(chat_id, "❌ Не нашла текстовых постов в этом файле. Нужен экспорт канала "
                           "(Telegram Desktop → ⋮ у канала → Экспорт истории чата → формат JSON).")
            return
        prompt = (
            f"Вот текущий файл style.md канала:\n\n{_style_md()}\n\n"
            f"Вот текущий файл examples.txt (примеры постов):\n\n{_examples_txt()}\n\n"
            f"Вот {len(posts)} новых постов из свежего экспорта канала, в хронологическом порядке от старых к новым:\n\n"
            + "\n\n".join(f"<post>\n{p}\n</post>" for p in posts)
            + "\n\nЗадача:\n"
            "1. Проверь, не изменился ли стиль канала по сравнению с текущим style.md (новые приёмы, рубрики, "
            "слова-паразиты, структура постов). Если есть заметные изменения — обнови style.md, сохранив его "
            "структуру и примерный объём; если изменений нет — верни style.md почти без изменений.\n"
            "2. Собери новый examples.txt: замени секцию самых свежих постов (в начале файла) на 20-25 самых "
            "свежих и показательных постов из присланных (как есть, в Telegram HTML), обновив в заголовке секции "
            f"указание на период — сейчас {time.strftime('%B %Y')}. Секции старых примеров по рубрикам ниже "
            "оставь без изменений.\n\n"
            "Ответь СТРОГО в формате, без пояснений до, между и после:\n"
            "===STYLE.MD===\n<полный новый текст style.md>\n===EXAMPLES.TXT===\n<полный новый текст examples.txt>"
        )
        body = {"model": model(), "max_tokens": 16000, "messages": [{"role": "user", "content": prompt}]}
        res = http("https://api.anthropic.com/v1/messages", body, headers=claude_headers(), timeout=280)
        text = "".join(b.get("text", "") for b in res["content"] if b.get("type") == "text")
        if "===STYLE.MD===" not in text or "===EXAMPLES.TXT===" not in text:
            raise RuntimeError("модель не вернула ожидаемый формат ответа")
        _, rest = text.split("===STYLE.MD===", 1)
        new_style, new_examples = rest.split("===EXAMPLES.TXT===", 1)
        new_style, new_examples = new_style.strip(), new_examples.strip()
        (HERE / "style.md").write_text(new_style, encoding="utf-8")
        (HERE / "examples.txt").write_text(new_examples, encoding="utf-8")
        committed = False
        try:
            committed = (
                github_commit_file("style.md", new_style, "Обновление style.md из нового экспорта канала")
                and github_commit_file("examples.txt", new_examples, "Обновление examples.txt из нового экспорта канала")
            )
        except Exception as e:
            log(f"Не удалось закоммитить обновлённый стиль на GitHub: {e}")
        drop_status(chat_id, status_id)
        where = "и сохранила в репозиторий на GitHub" if committed else "сохранила только локально — на GitHub нужно загрузить вручную через Upload files"
        send(chat_id, f"✅ Разобрала {len(posts)} постов из экспорта и обновила style.md и examples.txt, {where}.")
    except Exception as e:
        drop_status(chat_id, status_id)
        send(chat_id, f"❌ Не получилось обновить стиль: {str(e)[:300]}")
        raise


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

    style_doc = next(
        (m["document"] for m in group if "document" in m and not m["document"].get("mime_type", "").startswith("image/")),
        None,
    )
    if style_doc and (style_doc.get("file_name", "").lower().endswith(".json") or "/updatestyle" in notes.lower()):
        do_update_style(chat_id, style_doc, first["message_id"])
        return
    if notes.lower().startswith("/updatestyle"):
        send(chat_id, "Пришлите вместе с этой командой (или отдельно) файл result.json — экспорт истории канала из Telegram Desktop.")
        return

    reply = first.get("reply_to_message")
    is_edit = bool(reply and reply.get("from", {}).get("is_bot") and not any("photo" in m for m in group))
    has_photo = any("photo" in m or m.get("document", {}).get("mime_type", "").startswith("image/") for m in group)
    if is_edit:
        status_text = "✏️ Переписываю пост…"
    elif has_photo:
        n = sum(1 for m in group if "photo" in m or "document" in m)
        status_text = ("📸 Фото получено" if n == 1 else f"📸 Получено фото: {n}") + ". Ищу информацию и пишу пост, это займёт до минуты…"
    else:
        status_text = "⏳ Ищу информацию и пишу пост, это займёт до минуты…"
    status_id = None
    if is_edit or has_photo or notes:
        try:
            status_id = tg("sendMessage", chat_id=chat_id, text=status_text,
                           reply_parameters={"message_id": first["message_id"], "allow_sending_without_reply": True})["message_id"]
        except Exception:
            pass
    try:
        if is_edit:
            content = [{"type": "text", "text": f"Вот пост:\n\n{reply.get('text', '')}\n\nПерепиши его в стиле канала с учётом пожелания автора: {notes}\n\nЕсли автор дописала факты — используй их."}]
        else:
            photos = [m["photo"][-1]["file_id"] for m in group if "photo" in m][:MAX_IMAGES]
            photos += [m["document"]["file_id"] for m in group if m.get("document", {}).get("mime_type", "").startswith("image/")][: MAX_IMAGES - len(photos)]
            if not photos and not notes:
                drop_status(chat_id, status_id)
                send(chat_id, HELP)
                return
            content = []
            for fid in photos:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": download_photo(fid)}})
            task = "Напиши пост для канала по этим фото." + (f"\n\nЗаметки автора:\n{notes}" if notes else "\n\nЗаметок нет — опирайся на фото.")
            task += ("\n\nЕсли в заметках автора явно не указана рубрика (книга, косметика/уход, парфюм, "
                     "ресторан/еда, мероприятие, бокс/подарок, акция, лайфстайл) — определи её сама по содержанию "
                     "фото (обложка, упаковка, вывеска, интерьер и т.п.) и пиши строго по правилам этой рубрики из style.md.")
            if VARIANTS > 1:
                task += (f"\n\nСделай {VARIANTS} разных варианта подачи этого поста: разный заголовок и ракурс "
                          "изложения, но одни и те же факты и тот же стиль канала. Раздели варианты строкой "
                          f"{VARIANT_SEP} (каждый вариант — самостоятельный пост, без слова «вариант» внутри текста). "
                          "Если нужна уточняющая строка NOTE — напиши её один раз в самом конце, после последнего варианта.")
            content.append({"type": "text", "text": task})

        tg("sendChatAction", chat_id=chat_id, action="typing")
        post = ask_claude(content)
    except Exception as e:
        drop_status(chat_id, status_id)
        send(chat_id, f"❌ Не получилось сгенерировать пост: {str(e)[:300]}\nПопробуйте отправить ещё раз.")
        raise
    drop_status(chat_id, status_id)
    note = ""
    if "NOTE:" in post:
        post, note = post.split("NOTE:", 1)
        post, note = post.strip(), note.strip()
    variants = [p.replace("<post>", "").replace("</post>", "").strip() for p in post.split(VARIANT_SEP)]
    variants = [v for v in variants if v]
    if not variants:
        variants = [post.strip()]
    if len(variants) > 1:
        for i, v in enumerate(variants, 1):
            send(chat_id, f"<b>Вариант {i}</b>\n\n{v}", reply_to=first["message_id"])
    else:
        send(chat_id, variants[0], reply_to=first["message_id"])
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

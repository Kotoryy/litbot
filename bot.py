"""Telegram-бот, который сам публикует в канал статьи из litparser.

Раз в check_every_min минут запускает парсер и кладёт новые статьи в очередь,
раз в post_every_min минут публикует следующую статью из очереди в канал
(кроме тихих часов). Управляется командами в личке с ботом.

Запуск:
    python bot.py                            # постоянная работа (компьютер / сервер)
    python bot.py --once                     # один проход и выход (планировщик)
    python bot.py --once --state state.sql   # то же, база в текстовом файле (GitHub Actions)
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import sqlite3
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

from litparser import (Article, Fetcher, Filter, SeenStore, Telegram, lead_from_fulltext,
                       find_articles, parse_date, render_post, shorten)

log = logging.getLogger("litbot")

HELP = """<b>Команды</b>
/status — состояние бота и очереди
/queue — ближайшие посты
/check — проверить сайты прямо сейчас
/post — опубликовать следующий пост сейчас
/skip — выбросить следующий пост из очереди
/digest — показать, каким будет дайджест недели (только вам)
/pause — остановить публикацию
/resume — возобновить публикацию"""

# Какие обновления получать от Telegram (реакции — для рейтинга в дайджесте)
ALLOWED_UPDATES = json.dumps(["message", "callback_query", "message_reaction_count"])

# Ошибки Telegram, при которых дело не в посте, а в доступе к каналу
CHAT_ERRORS = ("chat not found", "not enough rights", "bot is not a member",
               "forbidden", "need administrator rights")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- queue

class Queue:
    """Очередь постов и состояние бота в той же SQLite-базе, что и парсер."""

    def __init__(self, path: str):
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    url TEXT UNIQUE, source TEXT, title TEXT, summary TEXT,
                    image TEXT, published TEXT,
                    status TEXT,   -- review | ready | posted | skipped | dropped | failed
                    added_at TEXT, posted_at TEXT);
                CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
            """)
            # колонки, добавленные позже, — для баз, созданных старой версией
            cols = {r[1] for r in self.db.execute("PRAGMA table_info(queue)")}
            for col, decl in (("message_id", "INTEGER"), ("reactions", "INTEGER DEFAULT 0"), ("topic", "TEXT")):
                if col not in cols:
                    self.db.execute(f"ALTER TABLE queue ADD COLUMN {col} {decl}")
            if "topic" not in cols:  # до появления тем бот писал только о литературе
                self.db.execute("UPDATE queue SET topic = 'literature'")
            self.db.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.db.execute(sql, params)
            self.db.commit()
            return cur

    def add(self, art: Article, status: str) -> int | None:
        cur = self._exec(
            "INSERT OR IGNORE INTO queue (url, source, title, summary, image, published,"
            " status, added_at, topic) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (art.url, art.source, art.title, art.summary, art.image,
             art.published.isoformat() if art.published else None, status, utcnow().isoformat(),
             art.topic),
        )
        return cur.lastrowid if cur.rowcount else None

    def get(self, qid: int) -> sqlite3.Row | None:
        return self._exec("SELECT * FROM queue WHERE id = ?", (qid,)).fetchone()

    def ready(self, limit: int = 1) -> list[sqlite3.Row]:
        return self._exec(
            "SELECT * FROM queue WHERE status = 'ready' ORDER BY id LIMIT ?", (limit,)).fetchall()

    def set_status(self, qid: int, status: str) -> None:
        posted = utcnow().isoformat() if status == "posted" else None
        self._exec("UPDATE queue SET status = ?, posted_at = ? WHERE id = ?", (status, posted, qid))

    def set_posted(self, qid: int, message_id: int | None) -> None:
        self._exec("UPDATE queue SET status = 'posted', posted_at = ?, message_id = ? WHERE id = ?",
                   (utcnow().isoformat(), message_id, qid))

    def set_reactions(self, message_id: int, count: int) -> None:
        self._exec("UPDATE queue SET reactions = ? WHERE message_id = ?", (count, message_id))

    def posted_since(self, since: datetime) -> list[sqlite3.Row]:
        """Опубликованное после since: сначала с большим числом реакций, потом свежее."""
        return self._exec(
            "SELECT * FROM queue WHERE status = 'posted' AND posted_at >= ?"
            " ORDER BY COALESCE(reactions, 0) DESC, posted_at DESC", (since.isoformat(),)).fetchall()

    def counts(self) -> dict[str, int]:
        rows = self._exec("SELECT status, COUNT(*) FROM queue GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}

    def trim(self, keep: int) -> int:
        """Оставляет в очереди не больше keep самых свежих статей каждой темы."""
        return self._exec(
            "UPDATE queue SET status = 'dropped' WHERE id IN ("
            " SELECT id FROM (SELECT id, ROW_NUMBER() OVER (PARTITION BY COALESCE(topic, '')"
            "   ORDER BY COALESCE(published, added_at) DESC) AS n FROM queue WHERE status = 'ready')"
            " WHERE n > ?)", (keep,)
        ).rowcount

    def last_posted_by_topic(self) -> dict[str, str]:
        rows = self._exec("SELECT COALESCE(topic, ''), MAX(posted_at) FROM queue"
                          " WHERE status = 'posted' GROUP BY COALESCE(topic, '')").fetchall()
        return {r[0]: r[1] for r in rows}

    def prune(self, days: int) -> None:
        """Удаляет давние записи, чтобы база не росла бесконечно."""
        cutoff = (utcnow() - timedelta(days=days)).isoformat()
        self._exec("DELETE FROM queue WHERE status NOT IN ('ready', 'review') AND added_at < ?", (cutoff,))
        try:
            self._exec("DELETE FROM seen WHERE added_at < ?", (cutoff,))
        except sqlite3.OperationalError:  # таблицы seen ещё нет
            pass

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self._exec("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def set_state(self, key: str, value: str) -> None:
        self._exec("INSERT OR REPLACE INTO state VALUES (?, ?)", (key, value))


def row_to_article(row: sqlite3.Row) -> Article:
    return Article(row["source"], row["title"], row["url"], row["summary"] or "",
                   row["image"] or "", parse_date(row["published"] or ""), topic=row["topic"] or "")


def parse_quiet(raw: str | None) -> tuple[dtime, dtime] | None:
    if not raw:
        return None
    start, end = (dtime.fromisoformat(p.strip()) for p in str(raw).split("-"))
    return start, end


# ---------------------------------------------------------------- bot

class Bot:
    def __init__(self, cfg: dict, token: str):
        self.cfg = cfg
        self.bcfg = cfg.get("bot") or {}
        self.tg_cfg = cfg["telegram"]
        self.channel = str(self.tg_cfg["chat_id"])
        self.api = Telegram(token, self.channel)
        self.queue = Queue(cfg.get("db_path", "seen.sqlite3"))
        self.flt = Filter(cfg)
        self.lead_skip = [Filter._rx(p) for p in cfg.get("lead_skip") or []]
        self.rubrics = [
            (r["tag"], re.compile(r["url"]) if r.get("url") else None,
             [Filter._rx(w) for w in r.get("words") or []], set(r.get("sources") or []))
            for r in cfg.get("rubrics") or []
        ]
        self.max_rubrics = self.tg_cfg.get("max_rubrics", 2)
        self.topic_tags = {name: t.get("tag", "") for name, t in (cfg.get("topics") or {}).items()}
        # сколько дней статья может ждать в очереди (у джаза новости редкие — нужен запас)
        self.queue_max_age = self.bcfg.get("max_age_in_queue_days", 7)
        self.digest_cfg = cfg.get("digest") or {}
        self.admins = {int(a) for a in self.bcfg.get("admins") or []}
        self.moderation = bool(self.bcfg.get("moderation", False))
        self.check_every = timedelta(minutes=self.bcfg.get("check_every_min", 60))
        self.post_every = timedelta(minutes=self.bcfg.get("post_every_min", 45))
        self.quiet = parse_quiet(self.bcfg.get("quiet_hours"))
        self.max_queue = self.bcfg.get("max_queue", 30)
        try:
            self.tz = ZoneInfo(self.bcfg["timezone"]) if self.bcfg.get("timezone") else None
        except Exception:
            log.warning("Неизвестный часовой пояс %r, беру системный", self.bcfg.get("timezone"))
            self.tz = None
        self.once = False            # режим «один проход и выход»
        self.force_check = False     # /check, пришедший в режиме --once
        self.check_reply: int | None = None
        self.checking = threading.Lock()
        self.next_check = time.monotonic()  # первая проверка сразу после старта
        self.retry_at = 0.0
        self.offset: int | None = None

    # ---------- Telegram helpers

    def call(self, method: str, **data) -> dict:
        return self.api._call(method, **data)

    def reply(self, chat_id: int, text: str, **extra) -> None:
        try:
            self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                      disable_web_page_preview="true", **extra)
        except Exception as exc:
            log.warning("не удалось ответить в %s: %s", chat_id, exc)

    def notify_admins(self, text: str) -> None:
        for admin in self.admins:
            self.reply(admin, text)

    def hashtags_for(self, art: Article) -> str:
        """Хэштег темы + рубрики по правилам из config.yaml (ссылка / слова в заголовке / источник)."""
        title = art.title.lower()
        topic_tag = self.topic_tags.get(art.topic, "")
        tags: list[str] = [topic_tag] if topic_tag else []
        limit = self.max_rubrics + len(tags)
        for tag, url_rx, words, sources in self.rubrics:
            if len(tags) >= limit:
                break
            if tag not in tags and ((url_rx and url_rx.search(art.url))
                                    or any(w.search(title) for w in words) or art.source in sources):
                tags.append(tag)
        extra = self.tg_cfg.get("hashtags", "")
        return " ".join(tags + ([extra] if extra and extra not in tags else [])).strip()

    def send_article(self, chat_id: str | int, art: Article, markup: dict | None = None) -> int | None:
        """Пост с картинкой, если она есть; при проблемах с картинкой — текстом. Возвращает id сообщения."""
        hashtags = self.hashtags_for(art)
        extra = {"reply_markup": json.dumps(markup)} if markup else {}
        if self.tg_cfg.get("with_photo", True) and art.image:
            try:
                r = self.call("sendPhoto", chat_id=chat_id, photo=art.image, parse_mode="HTML",
                              caption=render_post(art, hashtags, 1024), **extra)
                return (r.get("result") or {}).get("message_id")
            except RuntimeError as exc:
                if any(e in str(exc).lower() for e in CHAT_ERRORS):
                    raise
                log.debug("фото не ушло (%s), шлю текстом", exc)
        r = self.call("sendMessage", chat_id=chat_id, text=render_post(art, hashtags, 4096),
                      parse_mode="HTML", disable_web_page_preview="false", **extra)
        return (r.get("result") or {}).get("message_id")

    # ---------- schedule

    def paused(self) -> bool:
        return self.queue.get_state("paused") == "1"

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        if not self.quiet:
            return False
        t = (now or datetime.now(self.tz)).time()
        start, end = self.quiet
        return start <= t < end if start <= end else (t >= start or t < end)

    def last_post(self) -> datetime | None:
        raw = self.queue.get_state("last_post")
        return datetime.fromisoformat(raw) if raw else None

    def next_post_at(self) -> datetime:
        last = self.last_post()
        # ещё ни одного поста — можно публиковать сразу
        return last + self.post_every if last else datetime(2000, 1, 1, tzinfo=timezone.utc)

    def tick(self) -> None:
        if time.monotonic() >= self.next_check and not self.checking.locked():
            self.next_check = time.monotonic() + self.check_every.total_seconds()
            self.start_check()

        if not self.paused() and self.digest_due():
            self.send_digest()

        if (not self.paused() and not self.in_quiet_hours()
                and time.monotonic() >= self.retry_at and utcnow() >= self.next_post_at()):
            self.post_next()

    # ---------- parsing

    def start_check(self, reply_to: int | None = None) -> bool:
        if self.once:  # проверим синхронно после разбора команд
            self.force_check, self.check_reply = True, reply_to
            return True
        if self.checking.locked():
            return False
        threading.Thread(target=self.check, args=(reply_to,), daemon=True).start()
        return True

    def check(self, reply_to: int | None) -> None:
        with self.checking:
            log.info("Проверяю источники…")
            try:
                store = SeenStore(self.cfg.get("db_path", "seen.sqlite3"))
                fetcher = Fetcher(self.cfg.get("request_delay", 1.0))
                articles = find_articles(self.cfg, store, fetcher, self.flt)
                # берём не больше max_queue на тему (важно при первом запуске); find_articles
                # уже чередует источники, свежие первыми, — остальное просто запоминаем
                keep, per_topic = set(), {}
                for a in articles:
                    if per_topic.get(a.topic, 0) < self.max_queue:
                        keep.add(id(a))
                        per_topic[a.topic] = per_topic.get(a.topic, 0) + 1
                added = 0
                for art in articles:
                    qid = None
                    if id(art) in keep:
                        self.add_lead(art, fetcher)
                        qid = self.queue.add(art, "review" if self.moderation else "ready")
                    store.mark(art)
                    if qid:
                        added += 1
                        if self.moderation:
                            self.send_review(qid, art)
                store.db.close()
                dropped = self.queue.trim(self.max_queue)
                self.queue.set_state("last_check", utcnow().isoformat())
                msg = f"Проверка завершена: новых статей {added}"
                if dropped:
                    msg += f", из очереди вытеснено старых {dropped}"
                log.info(msg)
                if reply_to:
                    self.reply(reply_to, msg)
            except Exception as exc:
                log.exception("Ошибка проверки")
                if reply_to:
                    self.reply(reply_to, f"Ошибка проверки: {exc}")

    def add_lead(self, art: Article, fetcher: Fetcher) -> None:
        """Заменяет анонс сайта первыми абзацами статьи (если так настроено)."""
        if self.tg_cfg.get("summary", "lead") != "lead":
            return
        limit = self.tg_cfg.get("summary_chars", 600)
        try:
            lead = fetcher.article_lead(art.url, limit, self.lead_skip)
        except Exception as exc:
            log.debug("не удалось взять текст %s: %s", art.url, exc)
            lead = ""
        if not lead and art.fulltext:  # страница без текста (грузится скриптом) — берём из ленты
            lead = lead_from_fulltext(art.fulltext, limit, self.lead_skip)
        if lead:
            art.summary = lead

    def send_review(self, qid: int, art: Article) -> None:
        markup = {"inline_keyboard": [[
            {"text": "✅ В очередь", "callback_data": f"ok:{qid}"},
            {"text": "⚡ Сейчас", "callback_data": f"now:{qid}"},
            {"text": "❌ Пропустить", "callback_data": f"no:{qid}"},
        ]]}
        for admin in self.admins:
            try:
                self.send_article(admin, art, markup)
            except Exception as exc:
                log.warning("не удалось отправить на модерацию %s: %s", admin, exc)

    # ---------- posting

    def upcoming(self) -> list[sqlite3.Row]:
        """Очередь в порядке публикации: темы по кругу (дольше всех не выходившая — первой),
        внутри темы — в порядке поступления."""
        rows = self.queue.ready(1000)
        last = self.queue.last_posted_by_topic()
        by_topic: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            by_topic.setdefault(r["topic"] or "", []).append(r)
        order = sorted(by_topic, key=lambda t: last.get(t) or "")
        result: list[sqlite3.Row] = []
        while any(by_topic.values()):
            for t in order:
                if by_topic[t]:
                    result.append(by_topic[t].pop(0))
        return result

    def post_next(self) -> bool:
        """Публикует следующую пригодную статью из очереди. True — если что-то ушло."""
        while rows := self.upcoming()[:1]:
            row = rows[0]
            art = row_to_article(row)
            if self.flt.too_old(art, self.queue_max_age):
                self.queue.set_status(row["id"], "dropped")
                log.info("Устарело, пропускаю: %s", art.title)
                continue
            return self.post(row)
        return False

    def post(self, row: sqlite3.Row) -> bool:
        art = row_to_article(row)
        try:
            message_id = self.send_article(self.channel, art)
        except requests.RequestException as exc:
            log.warning("Сеть недоступна (%s), повторю через минуту", exc)
            self.retry_at = time.monotonic() + 60
            return False
        except RuntimeError as exc:
            if any(e in str(exc).lower() for e in CHAT_ERRORS):
                self.queue.set_state("paused", "1")
                log.error("Нет доступа к каналу %s: %s — публикация на паузе", self.channel, exc)
                self.notify_admins(
                    f"⚠️ Не могу писать в канал {self.channel}: {exc}\n"
                    "Проверьте, что бот — администратор канала с правом публикации, затем /resume.")
                return False
            self.queue.set_status(row["id"], "failed")
            log.error("Пост не принят Telegram («%s»): %s", art.title, exc)
            return False
        self.queue.set_posted(row["id"], message_id)
        self.queue.set_state("last_post", utcnow().isoformat())
        log.info("Опубликовано: %s", art.title)
        return True

    # ---------- updates

    def poll(self) -> list[dict]:
        params = {"timeout": 20, "allowed_updates": ALLOWED_UPDATES}
        if self.offset is not None:
            params["offset"] = self.offset
        try:
            updates = self.call("getUpdates", **params)["result"]
        except (requests.RequestException, RuntimeError) as exc:
            log.warning("getUpdates: %s", exc)
            time.sleep(5)
            return []
        if updates:
            self.offset = updates[-1]["update_id"] + 1
        return updates

    def handle(self, upd: dict) -> None:
        try:
            if "callback_query" in upd:
                self.on_callback(upd["callback_query"])
            elif "message_reaction_count" in upd:
                self.on_reactions(upd["message_reaction_count"])
            elif (msg := upd.get("message")) and msg.get("text"):
                self.on_message(msg)
        except Exception:
            log.exception("Ошибка обработки обновления")

    def on_message(self, msg: dict) -> None:
        if msg["chat"]["type"] != "private":
            return
        user, chat = msg["from"]["id"], msg["chat"]["id"]
        if user not in self.admins:
            self.reply(chat, f"Это бот канала {self.channel}.\n"
                             f"Ваш Telegram id: <code>{user}</code> — чтобы управлять ботом, "
                             "добавьте его в <code>bot.admins</code> в config.yaml и перезапустите бота.")
            return

        cmd = msg["text"].split()[0].split("@")[0].lower()
        if cmd in ("/start", "/help"):
            self.reply(chat, HELP)
        elif cmd == "/status":
            self.reply(chat, self.status_text())
        elif cmd == "/queue":
            self.reply(chat, self.queue_text())
        elif cmd == "/check":
            if self.start_check(reply_to=chat):
                self.reply(chat, "Проверяю источники, это займёт минуту-две…")
            else:
                self.reply(chat, "Проверка уже идёт.")
        elif cmd == "/post":
            self.reply(chat, "Опубликовано ✅" if self.post_next() else "Не получилось: очередь пуста или ошибка (см. лог).")
        elif cmd == "/skip":
            rows = self.upcoming()[:1]
            if rows:
                self.queue.set_status(rows[0]["id"], "skipped")
                self.reply(chat, f"Пропущено: {rows[0]['title']}")
            else:
                self.reply(chat, "Очередь пуста.")
        elif cmd == "/digest":
            text = self.digest_text()
            self.reply(chat, text or "За последнюю неделю опубликовано меньше 3 постов — дайджест не соберётся.")
        elif cmd == "/pause":
            self.queue.set_state("paused", "1")
            self.reply(chat, "⏸ Публикация остановлена. /resume — продолжить.")
        elif cmd == "/resume":
            self.queue.set_state("paused", "0")
            self.retry_at = 0
            self.reply(chat, "▶️ Публикация возобновлена.")
        else:
            self.reply(chat, "Не знаю такой команды.\n\n" + HELP)

    def on_callback(self, cq: dict) -> None:
        def answer(text: str) -> None:
            try:
                self.call("answerCallbackQuery", callback_query_id=cq["id"], text=text)
            except RuntimeError:  # в режиме --once нажатие могло «протухнуть» — не страшно
                pass

        if cq["from"]["id"] not in self.admins:
            answer("Нет доступа")
            return
        action, _, qid = cq.get("data", "").partition(":")
        row = self.queue.get(int(qid)) if qid.isdigit() else None
        if not row or row["status"] != "review":
            answer("Уже обработано")
        elif action == "ok":
            self.queue.set_status(row["id"], "ready")
            answer("Добавлено в очередь")
        elif action == "now":
            self.queue.set_status(row["id"], "ready")
            answer("Опубликовано" if self.post(self.queue.get(row["id"])) else "Ошибка публикации, пост в очереди")
        elif action == "no":
            self.queue.set_status(row["id"], "skipped")
            answer("Пропущено")
        # убираем кнопки с сообщения
        msg = cq.get("message")
        if msg:
            try:
                self.call("editMessageReplyMarkup", chat_id=msg["chat"]["id"], message_id=msg["message_id"])
            except RuntimeError:
                pass

    def on_reactions(self, upd: dict) -> None:
        """Число реакций на пост в канале — для рейтинга в дайджесте."""
        chat = upd.get("chat") or {}
        if str(chat.get("id")) != self.channel and f"@{chat.get('username', '')}".lower() != self.channel.lower():
            return
        total = sum(r.get("total_count", 0) for r in upd.get("reactions") or [])
        self.queue.set_reactions(upd["message_id"], total)

    # ---------- weekly digest

    def digest_due(self) -> bool:
        d = self.digest_cfg
        if not d.get("enabled"):
            return False
        now = datetime.now(self.tz)
        return (now.isoweekday() == int(d.get("weekday", 7))
                and now.time() >= dtime.fromisoformat(str(d.get("time", "19:00")))
                and self.queue.get_state("last_digest") != now.date().isoformat())

    def digest_text(self) -> str | None:
        """Лучшие посты недели: по реакциям, при равенстве — свежие; не больше per_source с сайта."""
        d = self.digest_cfg
        size, per_source = int(d.get("size", 5)), int(d.get("per_source", 2))
        picked: list[sqlite3.Row] = []
        by_source: dict[str, int] = {}
        for row in self.queue.posted_since(utcnow() - timedelta(days=7)):
            if by_source.get(row["source"], 0) >= per_source:
                continue
            picked.append(row)
            by_source[row["source"]] = by_source.get(row["source"], 0) + 1
            if len(picked) >= size:
                break
        if len(picked) < 3:
            return None

        e = html.escape
        today = datetime.now(self.tz)
        head = (f"<b>{e(d.get('title', '📚 Главное за неделю'))}</b>\n"
                f"<i>{today - timedelta(days=6):%d.%m} — {today:%d.%m}</i>")
        items = [f"{i}. <a href=\"{e(r['url'], quote=True)}\">{e(r['title'])}</a> — <i>{e(r['source'])}</i>"
                 for i, r in enumerate(picked, 1)]
        return "\n\n".join([head, *items, e(d.get("hashtags", "#дайджест"))])

    def send_digest(self) -> None:
        today = datetime.now(self.tz).date().isoformat()
        text = self.digest_text()
        if not text:
            log.info("Дайджест: за неделю меньше 3 постов, пропускаю")
            self.queue.set_state("last_digest", today)
            return
        try:
            self.call("sendMessage", chat_id=self.channel, text=text, parse_mode="HTML",
                      disable_web_page_preview="true")
        except (RuntimeError, requests.RequestException) as exc:
            log.error("Дайджест не отправлен: %s", exc)
            return
        self.queue.set_state("last_digest", today)
        self.queue.set_state("last_post", utcnow().isoformat())  # обычный пост — не сразу следом
        log.info("Дайджест недели опубликован")

    # ---------- texts

    def status_text(self) -> str:
        c = self.queue.counts()
        local = lambda dt: dt.astimezone(self.tz).strftime("%d.%m %H:%M")
        last_check = self.queue.get_state("last_check")
        last = self.last_post()
        lines = [
            "⏸ Публикация на паузе" if self.paused() else "▶️ Публикация включена",
            f"Канал: {self.channel}",
            f"Модерация: {'вкл' if self.moderation else 'выкл'}",
            "",
            f"В очереди: {c.get('ready', 0)}"
            + (f" · на модерации: {c.get('review', 0)}" if self.moderation else ""),
            f"Опубликовано всего: {c.get('posted', 0)}",
            "",
            "Последняя проверка: " + (local(datetime.fromisoformat(last_check)) if last_check else "ещё не было")
            + (" (идёт сейчас)" if self.checking.locked() else ""),
            "Последний пост: " + (local(last) if last else "ещё не было"),
        ]
        if not self.paused() and c.get("ready"):
            nxt = max(self.next_post_at(), utcnow())
            when = "сейчас" if nxt <= utcnow() else local(nxt)
            if self.in_quiet_hours():
                when += f" (сейчас тихие часы до {self.quiet[1]:%H:%M})"
            lines.append(f"Следующий пост: {when}")
        return "\n".join(lines)

    def queue_text(self) -> str:
        rows = self.upcoming()
        if not rows:
            return "Очередь пуста. /check — поискать новое."
        per_topic: dict[str, int] = {}
        for r in rows:
            tag = self.topic_tags.get(r["topic"] or "", "") or "без темы"
            per_topic[tag] = per_topic.get(tag, 0) + 1
        items = [f"{i}. {self.topic_tags.get(r['topic'] or '', '')} <i>{html.escape(r['source'])}</i> — "
                 f"{html.escape(shorten(r['title'], 90))}" for i, r in enumerate(rows[:10], 1)]
        summary = " · ".join(f"{t} {n}" for t, n in per_topic.items())
        return f"<b>Ближайшие посты</b>\nВ очереди: {summary}\n\n" + "\n".join(items)

    # ---------- main loop

    def startup(self) -> None:
        me = self.call("getMe")["result"]
        log.info("Бот @%s запущен. Канал: %s", me["username"], self.channel)
        try:
            self.call("getChat", chat_id=self.channel)
        except Exception as exc:
            log.warning("Канал %s недоступен боту (%s). Добавьте бота администратором канала.", self.channel, exc)
        if not self.admins:
            log.warning("bot.admins пуст — команды управления недоступны. "
                        "Напишите боту /start, он пришлёт ваш id.")
        if self.moderation and not self.admins:
            log.warning("Модерация включена, но админов нет — статьи некому одобрять.")

    def run(self) -> None:
        self.startup()
        while True:
            self.tick()
            for upd in self.poll():
                self.handle(upd)

    def run_once(self) -> None:
        """Один проход: команды из лички → проверка сайтов (если пора) → пост (если пора)."""
        self.once = True
        self.startup()
        slack = timedelta(minutes=5)  # запуски по расписанию могут опаздывать

        # команды и нажатия кнопок, пришедшие с прошлого запуска
        updates = self.call("getUpdates", timeout=0,
                            allowed_updates=ALLOWED_UPDATES)["result"]
        for upd in updates:
            self.handle(upd)
        if updates:  # подтверждаем, чтобы не обработать повторно
            self.call("getUpdates", timeout=0, offset=updates[-1]["update_id"] + 1)
            log.info("Обработано сообщений боту: %d", len(updates))

        last_check = self.queue.get_state("last_check")
        if (self.force_check or not last_check
                or utcnow() - datetime.fromisoformat(last_check) >= self.check_every - slack):
            self.check(self.check_reply)

        if not self.paused() and self.digest_due():
            self.send_digest()

        if self.paused():
            log.info("Публикация на паузе")
        elif self.in_quiet_hours():
            log.info("Тихие часы — не публикую")
        elif utcnow() >= self.next_post_at() - slack:
            if not self.post_next():
                log.info("Публиковать нечего")
        else:
            log.info("Следующий пост не раньше %s", self.next_post_at().astimezone(self.tz).strftime("%H:%M"))
        self.queue.prune(days=60)


def load_token(base: Path) -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token and (f := base / "token.txt").exists():
        token = f.read_text(encoding="utf-8").strip()
    if not token:
        sys.exit("Не найден токен: задайте TELEGRAM_BOT_TOKEN или положите его в token.txt рядом с bot.py")
    return token


def main() -> None:
    ap = argparse.ArgumentParser(description="Бот автопостинга литературных статей")
    ap.add_argument("-c", "--config", default="config.yaml")
    ap.add_argument("--once", action="store_true",
                    help="один проход и выход (для GitHub Actions или планировщика)")
    ap.add_argument("--state", metavar="FILE",
                    help="хранить базу в текстовом SQL-файле (удобно для git)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("urllib3", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg_path = Path(args.config).resolve()
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    # путь к базе — относительно конфига, чтобы бот работал из любой папки
    cfg["db_path"] = str(cfg_path.parent / cfg.get("db_path", "seen.sqlite3"))

    state = Path(args.state).resolve() if args.state else None
    if state:
        # рабочая база — временная, восстанавливается из текстового файла
        cfg["db_path"] = str(Path(tempfile.mkdtemp()) / "state.sqlite3")
        if state.exists():
            con = sqlite3.connect(cfg["db_path"])
            con.executescript(state.read_text(encoding="utf-8"))
            con.close()

    bot = Bot(cfg, load_token(Path(__file__).resolve().parent))
    try:
        bot.run_once() if args.once else bot.run()
    except RuntimeError as exc:
        sys.exit(f"Telegram отказал: {exc}. Проверьте токен в token.txt / TELEGRAM_BOT_TOKEN.")
    except KeyboardInterrupt:
        log.info("Остановлено.")
    finally:
        if state:
            bot.queue.db.close()
            con = sqlite3.connect(cfg["db_path"])
            state.write_text("\n".join(con.iterdump()) + "\n", encoding="utf-8")
            con.close()


if __name__ == "__main__":
    main()

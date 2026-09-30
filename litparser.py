"""Парсер литературных статей для Telegram-канала.

Собирает статьи из RSS-лент и HTML-страниц, фильтрует по ключевым словам,
отбрасывает уже виденные и публикует в Telegram (или выводит/сохраняет).

Примеры:
    python litparser.py                  # показать найденное, ничего не отмечать
    python litparser.py --init           # отметить всё текущее как виденное (первый запуск)
    python litparser.py --export posts.md
    python litparser.py --send           # отправить в Telegram
    python litparser.py --send --loop 60 # проверять каждые 60 минут
"""

from __future__ import annotations

import argparse
import html
import logging
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import requests
import yaml
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LitParser/1.0",
    "Accept-Language": "ru,en;q=0.8",
}
TG_API = "https://api.telegram.org/bot{token}/{method}"

log = logging.getLogger("litparser")


@dataclass
class Article:
    source: str
    title: str
    url: str
    summary: str = ""
    image: str = ""
    published: datetime | None = None
    score: int = 0
    topic: str = ""


# ---------------------------------------------------------------- utils

def clean_text(raw: str) -> str:
    text = BeautifulSoup(raw or "", "html.parser").get_text(" ")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_url(url: str) -> str:
    """Убирает utm-метки, якоря и хвостовой слэш — для дедупликации."""
    parts = urlsplit(url.strip())
    query = "&".join(
        p for p in parts.query.split("&") if p and not p.startswith(("utm_", "from=", "ref="))
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower().removeprefix("www."), path, query, ""))


def title_key(title: str) -> str:
    return re.sub(r"[^\w]+", "", title.lower())[:80]


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:—-") + "…"


# ---------------------------------------------------------------- storage

class SeenStore:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS seen ("
            " url TEXT PRIMARY KEY, title_key TEXT, source TEXT, added_at TEXT)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_title ON seen(title_key)")

    def is_seen(self, art: Article) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM seen WHERE url = ? OR title_key = ? LIMIT 1",
            (normalize_url(art.url), title_key(art.title)),
        ).fetchone()
        return row is not None

    def mark(self, art: Article) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO seen VALUES (?, ?, ?, ?)",
            (normalize_url(art.url), title_key(art.title), art.source,
             datetime.now(timezone.utc).isoformat()),
        )
        self.db.commit()


# ---------------------------------------------------------------- fetching

class Fetcher:
    def __init__(self, delay: float):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self.delay = delay
        self._last = 0.0

    def get(self, url: str) -> requests.Response:
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        resp = self.session.get(url, timeout=20)
        resp.raise_for_status()
        return resp

    def page_meta(self, url: str) -> dict:
        """Достаёт og:title / og:description / og:image / дату со страницы статьи."""
        soup = BeautifulSoup(self.get(url).content, "html.parser")

        def meta(*names: str) -> str:
            for n in names:
                tag = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
                if tag and tag.get("content"):
                    return tag["content"].strip()
            return ""

        title = meta("og:title", "twitter:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
        date_raw = meta("article:published_time", "pubdate", "date")
        if not date_raw and (t := soup.find("time", attrs={"datetime": True})):
            date_raw = t["datetime"]
        return {
            "title": clean_text(title),
            "summary": clean_text(meta("og:description", "description", "twitter:description")),
            "image": urljoin(url, meta("og:image", "twitter:image")) if meta("og:image", "twitter:image") else "",
            "published": parse_date(date_raw),
        }


    def article_lead(self, url: str, limit: int = 600, skip: list[re.Pattern] = ()) -> str:
        """Первые абзацы основного текста статьи (не больше ~limit символов)."""
        return extract_lead(self.get(url).content, limit, skip)


# Абзацы-«мусор»: подписи к фото, копирайты, призывы подписаться
JUNK_RE = re.compile(
    r"^(фото|иллюстрация|источник|автор|текст|обложка|подпишитесь|читайте также|реклама)\b"
    r"|©|cookie|javascript", re.I)


def extract_lead(raw_html: bytes | str, limit: int = 600, skip: list[re.Pattern] = ()) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside",
                     "form", "figure", "figcaption", "button", "iframe"]):
        tag.decompose()

    # основной текст — тот контейнер, где больше всего текста в <p>
    weight: dict[int, int] = {}
    parents: dict[int, object] = {}
    for p in soup.find_all("p"):
        parent = p.parent
        weight[id(parent)] = weight.get(id(parent), 0) + len(p.get_text(" ", strip=True))
        parents[id(parent)] = parent
    if not weight:
        return ""
    body = parents[max(weight, key=weight.get)]

    paragraphs: list[str] = []
    total = 0
    for p in body.find_all("p", recursive=False) or body.find_all("p"):
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        if len(text) < 60 or JUNK_RE.search(text) or any(rx.search(text.lower()) for rx in skip):
            continue
        if paragraphs and total + len(text) > limit:
            break
        paragraphs.append(text)
        total += len(text)
        if total >= limit * 0.6:
            break
    return shorten("\n\n".join(paragraphs), limit)


def parse_date(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def collect_rss(src: dict, fetcher: Fetcher) -> list[Article]:
    feed = feedparser.parse(fetcher.get(src["url"]).content)
    items = []
    for e in feed.entries:
        if not e.get("link") or not e.get("title"):
            continue
        published = None
        if st := (e.get("published_parsed") or e.get("updated_parsed")):
            published = datetime(*st[:6], tzinfo=timezone.utc)
        image = ""
        for key in ("media_content", "media_thumbnail"):
            if e.get(key):
                image = e[key][0].get("url", "")
                break
        if not image:
            for enc in e.get("enclosures", []):
                if enc.get("type", "").startswith("image"):
                    image = enc.get("href", "")
                    break
        summary = clean_text(e.get("summary", "") or e.get("description", ""))
        items.append(Article(src["name"], clean_text(e.title), e.link, summary, image, published))
    return items


def collect_html(src: dict, fetcher: Fetcher, limit: int = 20) -> list[Article]:
    soup = BeautifulSoup(fetcher.get(src["url"]).content, "html.parser")
    pattern = re.compile(src["link_pattern"])
    links: list[str] = []
    for a in soup.select("a[href]"):
        href = urljoin(src["url"], a["href"]).split("#")[0]
        if pattern.search(href) and href not in links:
            links.append(href)
    return [Article(src["name"], "", url) for url in links[:limit]]


# ---------------------------------------------------------------- filtering

class Filter:
    def __init__(self, cfg: dict):
        # темы: {имя: [регулярки]}; порядок в конфиге = приоритет при равном счёте
        topics = cfg.get("topics") or {"": {"keywords": cfg.get("keywords", [])}}
        self.topics = {name: [self._rx(k) for k in t.get("keywords") or []] for name, t in topics.items()}
        # «общая» тема (культура) достаётся статье, только если не подошла ни одна конкретная
        self.generic = {name for name, t in topics.items() if t.get("generic")}
        # исключения только для статей этой темы (например, поп-музыка в «культуре»)
        self.topic_exclude = {name: [self._rx(k) for k in t.get("exclude") or []] for name, t in topics.items()}
        self.exclude = [self._rx(k) for k in cfg.get("exclude", [])]
        self.min_score = cfg.get("min_score", 1)
        self.max_age = timedelta(days=cfg.get("max_age_days", 3))

    @staticmethod
    def _rx(word: str) -> re.Pattern:
        word = str(word).lower()
        # «re:...» — готовое регулярное выражение
        if word.startswith("re:"):
            return re.compile(r"(?<!\w)" + word[3:])
        # иначе совпадение с начала слова: «роман» найдёт «романа», но не «экстраромантик»
        return re.compile(r"(?<!\w)" + re.escape(word))

    def excluded(self, art: Article) -> bool:
        text = f"{art.title} {art.summary}".lower()
        return any(rx.search(text) for rx in self.exclude)

    def classify(self, art: Article, allowed: list[str] | None = None) -> tuple[str, int]:
        """Самая подходящая тема и её счёт (число совпадений ключевых слов)."""
        text = f"{art.title} {art.summary}".lower()
        best: dict[bool, tuple[str, int]] = {False: ("", 0), True: ("", 0)}
        for name, rxs in self.topics.items():
            if allowed and name not in allowed:
                continue
            score = sum(len(rx.findall(text)) for rx in rxs)
            generic = name in self.generic
            if score > best[generic][1]:
                best[generic] = (name, score)
        return best[False] if best[False][1] else best[True]

    def topic_excluded(self, art: Article) -> bool:
        text = f"{art.title} {art.summary}".lower()
        return any(rx.search(text) for rx in self.topic_exclude.get(art.topic, []))

    def too_old(self, art: Article, days: float | None = None) -> bool:
        limit = timedelta(days=days) if days else self.max_age
        return art.published is not None and datetime.now(timezone.utc) - art.published > limit


# ---------------------------------------------------------------- pipeline

def accept(art: Article, src: dict, flt: Filter) -> bool:
    """Решает, брать ли статью, и проставляет ей тему.

    topic: X          — профильный сайт: всё подряд, тема X
    filter: true      — только если нашлись ключевые слова какой-то темы (из topics, если задан список)
    fallback_topic: X — вместе с filter: не совпавшее тоже брать, с темой X
    """
    if not src.get("filter", False):
        art.topic = src.get("topic", "")
        art.score = flt.classify(art)[1]
    else:
        art.topic, art.score = flt.classify(art, src.get("topics"))
        if art.score < src.get("min_score", flt.min_score):
            if not src.get("fallback_topic"):
                return False
            art.topic = src["fallback_topic"]
    return not flt.topic_excluded(art)


def find_articles(cfg: dict, store: SeenStore, fetcher: Fetcher, flt: Filter) -> list[Article]:
    by_source: dict[str, list[Article]] = {}
    batch_keys: set[str] = set()
    with_photo = cfg["telegram"].get("with_photo", True)

    for src in cfg["sources"]:
        try:
            if src.get("type", "rss") == "html":
                raw = collect_html(src, fetcher)
            else:
                raw = collect_rss(src, fetcher)
        except Exception as exc:
            log.warning("%s: не удалось загрузить (%s)", src["name"], exc)
            continue

        accepted = 0
        for art in raw:
            if normalize_url(art.url) in batch_keys:
                continue
            if art.title and store.is_seen(art):
                continue

            # HTML-источники и RSS без описания — дотягиваем мету со страницы до фильтра
            enriched = False
            if not art.title or not art.summary:
                if not enrich(art, fetcher) and not art.title:
                    continue
                enriched = True
                if store.is_seen(art):
                    continue

            if flt.too_old(art, src.get("max_age_days")) or flt.excluded(art):
                continue
            if not accept(art, src, flt):
                continue

            # картинку ищем только для прошедших фильтр
            if with_photo and not art.image and not enriched:
                enrich(art, fetcher)

            batch_keys.add(normalize_url(art.url))
            by_source.setdefault(src["name"], []).append(art)
            accepted += 1
        log.info("%-25s получено %3d, подходит %3d", src["name"], len(raw), accepted)

    # свежие сверху внутри источника, затем по очереди из каждого — для разнообразия
    now = datetime.now(timezone.utc)
    queues = [sorted(v, key=lambda a: a.published or now, reverse=True) for v in by_source.values()]
    found: list[Article] = []
    while any(queues):
        for q in queues:
            if q:
                found.append(q.pop(0))
    return found


def enrich(art: Article, fetcher: Fetcher) -> bool:
    try:
        m = fetcher.page_meta(art.url)
    except Exception as exc:
        log.debug("%s: %s (%s)", art.source, art.url, exc)
        return False
    art.title = art.title or m["title"]
    art.summary = art.summary or m["summary"]
    art.image = art.image or m["image"]
    art.published = art.published or m["published"]
    return bool(art.title)


# ---------------------------------------------------------------- output

def render_post(art: Article, hashtags: str, limit: int) -> str:
    e = html.escape
    parts = [f"<b>{e(art.title)}</b>"]
    head_len = len(art.title) + len(art.source) + len(hashtags) + 60
    if art.summary and art.summary.lower() != art.title.lower():
        parts.append(e(shorten(art.summary, max(100, limit - head_len))))
    parts.append(f'📖 <a href="{e(art.url, quote=True)}">Читать в «{e(art.source)}»</a>')
    if hashtags:
        parts.append(e(hashtags))
    return "\n\n".join(parts)


def render_markdown(art: Article) -> str:
    date = art.published.strftime("%d.%m.%Y") if art.published else "—"
    lines = [f"### {art.title}", f"*{art.source} · {date} · score {art.score}*", ""]
    if art.summary:
        lines += [shorten(art.summary, 600), ""]
    lines += [art.url, ""]
    return "\n".join(lines)


class Telegram:
    def __init__(self, token: str, chat_id: str):
        self.token, self.chat_id = token, chat_id

    def _call(self, method: str, **data) -> dict:
        r = requests.post(TG_API.format(token=self.token, method=method), data=data, timeout=30)
        body = r.json()
        if not body.get("ok"):
            raise RuntimeError(body.get("description", r.text))
        return body

    def send(self, art: Article, hashtags: str, with_photo: bool) -> None:
        if with_photo and art.image:
            try:
                self._call("sendPhoto", chat_id=self.chat_id, photo=art.image,
                           caption=render_post(art, hashtags, 1024), parse_mode="HTML")
                return
            except RuntimeError as exc:
                log.debug("фото не ушло (%s), шлю текстом", exc)
        self._call("sendMessage", chat_id=self.chat_id, text=render_post(art, hashtags, 4096),
                   parse_mode="HTML", disable_web_page_preview="false")


# ---------------------------------------------------------------- main

def run_once(cfg: dict, args: argparse.Namespace) -> None:
    store = SeenStore(cfg.get("db_path", "seen.sqlite3"))
    fetcher = Fetcher(cfg.get("request_delay", 1.0))
    articles = find_articles(cfg, store, fetcher, Filter(cfg))
    tg_cfg = cfg["telegram"]
    hashtags = tg_cfg.get("hashtags", "")

    if args.init:
        for art in articles:
            store.mark(art)
        log.info("Отмечено как виденное: %d. Следующие запуски покажут только новое.", len(articles))
        return

    batch = articles[: cfg.get("max_posts_per_run", 10)]
    log.info("Новых статей: %d, к публикации: %d", len(articles), len(batch))

    if args.send:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            sys.exit("Не задан TELEGRAM_BOT_TOKEN")
        tg = Telegram(token, str(tg_cfg["chat_id"]))
        for art in batch:
            try:
                tg.send(art, hashtags, tg_cfg.get("with_photo", True))
                store.mark(art)
                log.info("Отправлено: %s", art.title)
            except Exception as exc:
                log.error("Ошибка отправки «%s»: %s", art.title, exc)
            time.sleep(tg_cfg.get("send_delay", 3))
    elif args.export:
        path = Path(args.export)
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n## Подборка {datetime.now():%d.%m.%Y %H:%M}\n\n")
            for art in batch:
                f.write(render_markdown(art) + "\n")
                store.mark(art)
        log.info("Записано в %s", path)
    else:
        for i, art in enumerate(batch, 1):
            date = art.published.strftime("%d.%m %H:%M") if art.published else "без даты"
            print(f"\n{i}. [{art.source} · {date} · score {art.score}]")
            print(render_post(art, hashtags, 600))
        print("\n(режим просмотра: ничего не отмечено как виденное)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Парсер литературных статей для Telegram")
    ap.add_argument("-c", "--config", default="config.yaml")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--send", action="store_true", help="отправить в Telegram")
    mode.add_argument("--export", metavar="FILE", help="дописать подборку в Markdown-файл")
    mode.add_argument("--init", action="store_true", help="отметить всё текущее как виденное")
    ap.add_argument("--loop", type=int, metavar="MIN", help="повторять каждые MIN минут")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("urllib3", "charset_normalizer"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    while True:
        run_once(cfg, args)
        if not args.loop:
            break
        log.info("Следующая проверка через %d мин.", args.loop)
        time.sleep(args.loop * 60)


if __name__ == "__main__":
    main()

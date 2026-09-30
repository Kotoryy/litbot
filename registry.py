"""Проверка источников по спискам иноагентов, нежелательных и экстремистских организаций.

Откуда берутся списки:
  - экстремистские организации — официальный перечень Минюста (minjust.gov.ru);
  - иноагенты — таблицы реестра в статье Википедии «Список иностранных агентов»;
  - нежелательные организации — категория Википедии с такими организациями.

Официальные реестры иноагентов и нежелательных организаций (reestrs.minjust.gov.ru)
недоступны с зарубежных серверов, где работает бот, поэтому для них используется
Википедия. Это НЕОФИЦИАЛЬНЫЙ источник: он может отставать или ошибаться.
Проверка — сигнал тревоги, а не юридическая гарантия.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

log = logging.getLogger("litbot.registry")

WIKI_API = "https://ru.wikipedia.org/w/api.php"
WIKI_HEADERS = {"User-Agent": "LitBot/1.0 (https://github.com/Kotoryy/litbot)"}
BROWSER_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "ru"}

EXTREMIST_URL = "https://minjust.gov.ru/ru/documents/7822/"
FOREIGN_AGENTS_PAGE = "Список иностранных агентов"
UNDESIRABLE_CATEGORY = ("Категория:Неправительственные организации, деятельность которых "
                        "признана нежелательной на территории Российской Федерации")

LIST_NAMES = {
    "extremist": "экстремистские организации (Минюст)",
    "foreign_agents": "иностранные агенты (по Википедии)",
    "undesirable": "нежелательные организации (по Википедии)",
}

DATE_RE = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")
# строка реестра о человеке: «123 Фамилия Имя Отчество …» — издание в ней лишь упоминается
PERSON_RE = re.compile(r"^\d+\s+[А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?\s+[А-ЯЁ][а-яё]+"
                       r"(?:\s+[А-ЯЁ][а-яё]+(?:вич|вна|ич|ична|кызы|оглы))?\s+[а-яё]")


def norm(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"«\s+", "«", text)
    text = re.sub(r"\s+»", "»", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------- загрузка списков

def fetch_extremist() -> list[str]:
    r = requests.get(EXTREMIST_URL, headers=BROWSER_HEADERS, timeout=60)
    r.raise_for_status()
    text = BeautifulSoup(r.content, "html.parser").get_text(" ", strip=True)
    start = text.find("Текст документа")
    if start < 0:
        raise ValueError("не найден текст перечня на странице Минюста")
    # пункты вида «1. Название (решение суда ...)»
    items = re.split(r"\s(?=\d{1,4}\.\s)", text[start:])
    entries = [i.strip() for i in items if re.match(r"\d{1,4}\.\s", i.strip())]
    if len(entries) < 50:
        raise ValueError(f"подозрительно короткий перечень ({len(entries)} пунктов)")
    return entries


def fetch_foreign_agents() -> list[str]:
    r = requests.get(WIKI_API, params={"action": "parse", "page": FOREIGN_AGENTS_PAGE, "prop": "text",
                                       "format": "json", "formatversion": 2, "redirects": 1},
                     headers=WIKI_HEADERS, timeout=60)
    r.raise_for_status()
    soup = BeautifulSoup(r.json()["parse"]["text"], "html.parser")
    for junk in soup.select(".reflist, .references, sup.reference, .navbox"):
        junk.decompose()
    entries = []
    for tr in soup.find_all("tr"):
        row = re.sub(r"\s+", " ", tr.get_text(" ", strip=True))
        # две даты — включён и уже исключён из реестра; строки о людях не нужны
        if (row and not row.startswith("№") and len(DATE_RE.findall(row)) < 2
                and not PERSON_RE.match(row)):
            entries.append(row)
    if len(entries) < 100:
        raise ValueError(f"подозрительно короткий список ({len(entries)} строк)")
    return entries


def fetch_undesirable() -> list[str]:
    titles, params = [], {"action": "query", "list": "categorymembers", "cmtitle": UNDESIRABLE_CATEGORY,
                          "cmlimit": 500, "format": "json"}
    while True:
        r = requests.get(WIKI_API, params=params, headers=WIKI_HEADERS, timeout=60)
        r.raise_for_status()
        data = r.json()
        titles += [m["title"] for m in data["query"]["categorymembers"] if m.get("ns") == 0]
        if "continue" not in data:
            break
        params.update(data["continue"])
    if len(titles) < 30:
        raise ValueError(f"подозрительно короткий список ({len(titles)} организаций)")
    return titles


FETCHERS = {"extremist": fetch_extremist, "foreign_agents": fetch_foreign_agents,
            "undesirable": fetch_undesirable}


def fetch_all() -> tuple[dict[str, list[str]], dict[str, str]]:
    """Загружает все списки. Возвращает (списки, ошибки загрузки)."""
    lists, errors = {}, {}
    for key, fetch in FETCHERS.items():
        try:
            lists[key] = fetch()
            log.info("Реестр «%s»: %d записей", LIST_NAMES[key], len(lists[key]))
        except Exception as exc:
            errors[key] = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("Реестр «%s» не загрузился: %s", LIST_NAMES[key], errors[key])
    return lists, errors


# ---------------------------------------------------------------- сверка

def source_aliases(src: dict) -> tuple[list[str], list[str]]:
    """Названия и домены, под которыми источник может значиться в списках."""
    base = re.sub(r"[«»\"]", "", src["name"].split(" — ")[0]).strip()
    names = {base, *src.get("aliases", [])}
    domain = urlsplit(src["url"]).netloc.lower().removeprefix("www.")
    domains = {domain, *[d.lower() for d in src.get("domains", [])]}
    return [norm(n) for n in names if n], [d for d in domains if d]


def find_matches(src: dict, lists: dict[str, list[str]]) -> list[tuple[str, str]]:
    """Совпадения источника со списками: [(ключ списка, запись)].

    Чтобы не ловить случайные слова («Культура», «Нож»), название должно стоять
    в кавычках («Нож») или совпадать с названием организации целиком; домен
    сайта ищется как есть.
    """
    names, domains = source_aliases(src)
    found = []
    for key, entries in lists.items():
        for entry in entries:
            e = norm(entry)
            title = re.sub(r"\s*\(.*?\)\s*$", "", e)  # «Дождь (телеканал)» → «дождь»
            bare = re.sub(r"[«»\"]", "", title)        # «Радио «Свобода»» → «радио свобода»
            hit = any(d in e for d in domains) or any(
                f"«{n}»" in e or f'"{n}"' in e or n in (title, bare) or title.startswith(n + " —")
                for n in names)
            if hit:
                found.append((key, entry[:300]))
    return found


def check_sources(sources: list[dict]) -> tuple[dict[str, list[tuple[str, str]]], dict[str, str], dict[str, int]]:
    """Сверяет источники со всеми списками.

    Возвращает (совпадения по источникам, ошибки загрузки, размеры списков).
    """
    lists, errors = fetch_all()
    matches = {}
    for src in sources:
        if hits := find_matches(src, lists):
            matches[src["name"]] = hits
    return matches, errors, {k: len(v) for k, v in lists.items()}

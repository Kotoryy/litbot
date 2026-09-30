"""Пробный прогон парсера без Telegram: что нашлось, какие темы и хэштеги, какой текст поста."""
import logging, os, sys, tempfile, yaml
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")
logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
import bot as B
from litparser import Fetcher, SeenStore, find_articles

cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
cfg["db_path"] = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
b = B.Bot(cfg, "x")
fetcher = Fetcher(0.5)
arts = find_articles(cfg, SeenStore(cfg["db_path"]), fetcher, b.flt)

print("\n=== ПО ТЕМАМ")
counts = {}
for a in arts:
    counts[a.topic] = counts.get(a.topic, 0) + 1
print(counts)

print("\n=== ВСЕ СТАТЬИ")
for a in arts:
    print(f"{b.hashtags_for(a):40} | {a.source[:22]:22} | {a.title[:70]}")

print("\n=== ПРИМЕРЫ ПОСТОВ (по 1 с каждого источника)")
seen = set()
for a in arts:
    if a.source in seen:
        continue
    seen.add(a.source)
    b.add_lead(a, fetcher)
    print(f"\n----- {a.source}\n{B.render_post(a, b.hashtags_for(a), 1024)}")

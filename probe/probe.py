import sys, feedparser, requests, time
from concurrent.futures import ThreadPoolExecutor
sys.stdout.reconfigure(encoding="utf-8")
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LitParser/1.0", "Accept-Language": "ru"}
urls = [l.strip() for l in open("probe/urls.txt", encoding="utf-8") if l.strip() and not l.startswith("#")]
def probe(u):
    try:
        r = requests.get(u, headers=H, timeout=20)
        f = feedparser.parse(r.content)
        n = len(f.entries)
        if not n: return f"XX {u} [{r.status_code}] нет записей"
        e = f.entries[0]; st = e.get("published_parsed") or e.get("updated_parsed")
        date = time.strftime("%d.%m", st) if st else "??"
        titles = " || ".join(x.get("title","")[:50] for x in f.entries[:4])
        return f"OK {u} [{n} шт, свежая {date}] {titles}"
    except Exception as ex:
        return f"XX {u} {type(ex).__name__}: {str(ex)[:60]}"
with ThreadPoolExecutor(12) as ex:
    for line in ex.map(probe, urls): print(line)

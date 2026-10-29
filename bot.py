"""
News → Telegram channel bot.

Collects fresh articles from Portuguese news sites, keeps only news that concern
Portugal, rewrites them with Claude into a short post in the chosen language and
publishes them to a Telegram channel. News about immigration / AIMA / Russians,
Ukrainians, Belarusians get the #важно tag. No posting during quiet hours
(news found at night is queued for the morning), and a daily digest of the main
news is posted at the start of quiet hours. Runs on a schedule (GitHub Actions).

Env variables:
  TELEGRAM_BOT_TOKEN   required — token from @BotFather
  TELEGRAM_CHANNEL     required — @channel_username or numeric chat id (-100...)
  ANTHROPIC_API_KEY    optional — enables rewriting, the Portugal filter and the
                       smart #важно detection (without it: keywords only)
  POST_LANGUAGE        optional — language for rewritten posts (default: русский)
  MAX_POSTS_PER_RUN    optional — total cap per run (default: 5)
  KEYWORDS             optional — comma-separated; only matching news are posted
  TIMEZONE             optional — default Europe/Lisbon
  QUIET_START          optional — no posts from this time (default 21:00)
  QUIET_END            optional — ... until this time (default 08:30)
  DIGEST_TIME          optional — daily digest time (default 21:00, "off" disables)
  DRY_RUN              optional — "1" prints posts instead of sending
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import feedparser
import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------- settings

SOURCES = [
    {
        "name": "Público",
        "rss": "https://feeds.feedburner.com/PublicoRSS",
        "home": "https://www.publico.pt/",
        "article_re": r"https://www\.publico\.pt/\d{4}/\d{2}/\d{2}/[^\"'#?]+",
    },
    {
        "name": "Observador",
        "rss": "https://observador.pt/feed/",
        "home": "https://observador.pt/",
        "article_re": r"https://observador\.pt/\d{4}/\d{2}/\d{2}/[^\"'#?]+",
    },
    {
        "name": "CNN Portugal",
        "rss": None,  # no public feed found — auto-discovery, then homepage parsing
        "home": "https://cnnportugal.iol.pt/",
        # e.g. /geral/some-title/20211122/619bfebb0cf21847f09c0374
        "article_re": r"(?:https://cnnportugal\.iol\.pt)?/[a-z0-9-]+/[a-z0-9-]+/\d{8}/[0-9a-f]{24}",
    },
]


def _hm(value: str) -> dtime:
    h, m = value.strip().split(":")
    return dtime(int(h), int(m))


STATE_FILE = Path(__file__).with_name("seen.json")      # links already processed
QUEUE_FILE = Path(__file__).with_name("state.json")     # night queue, today's posts, digest
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN") or "5")
POST_LANGUAGE = os.getenv("POST_LANGUAGE") or "русский"
KEYWORDS = [k.strip().lower() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]
DRY_RUN = os.getenv("DRY_RUN") == "1"
# manual run: post the N latest news right now, even if already seen
POST_LATEST = int(os.getenv("POST_LATEST") or "0")
# pause between posts in one run, minutes
POST_INTERVAL_MIN = float(os.getenv("POST_INTERVAL_MIN") or "5")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or "claude-haiku-5-5"

TZ = ZoneInfo(os.getenv("TIMEZONE") or "Europe/Lisbon")
QUIET_START = _hm(os.getenv("QUIET_START") or "21:00")
QUIET_END = _hm(os.getenv("QUIET_END") or "08:30")
_digest = (os.getenv("DIGEST_TIME") or "21:00").strip().lower()
DIGEST_TIME = None if _digest in ("off", "0", "no", "нет") else _hm(_digest)
DIGEST_MAX_ITEMS = int(os.getenv("DIGEST_MAX_ITEMS") or "10")
# what is kept in the queue (news found at night or not fitting into one run)
PENDING_MAX = int(os.getenv("PENDING_MAX") or "10")
PENDING_MAX_HOURS = float(os.getenv("PENDING_MAX_HOURS") or "14")

IMPORTANT_TAG = "#важно"
# used only when there is no Claude key (or Claude fails)
IMPORTANT_RE = re.compile(
    r"\b(aima|sef|imigra\w*|imigrant\w*|migra\w*|autoriza\w* de resid\w*|vistos?|"
    r"nacionalidade|refugiad\w*|asilo|r[uú]ss[iao]\w*|ucr[aâ]n\w*|bielorr[uú]ss\w*)",
    re.IGNORECASE,
)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsToTelegramBot/1.0)"}
TIMEOUT = 20

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]

# ---------------------------------------------------------------- state


def load_seen() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict) -> None:
    # keep the last 500 links per source so the file doesn't grow forever
    trimmed = {k: v[-500:] for k, v in seen.items()}
    STATE_FILE.write_text(json.dumps(trimmed, ensure_ascii=False, indent=1), encoding="utf-8")


def load_state() -> dict:
    state = {}
    if QUEUE_FILE.exists():
        try:
            state = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            state = {}
    state.setdefault("pending", [])
    state.setdefault("posted", [])
    state.setdefault("digest_date", "")
    return state


def save_state(state: dict, now: datetime) -> None:
    # keep only today's and yesterday's posts for the digest
    keep = {now.date().isoformat(), (now.date() - timedelta(days=1)).isoformat()}
    state["posted"] = [p for p in state["posted"] if p.get("date") in keep]
    QUEUE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


def clean(text: str) -> str:
    """Decode HTML entities (even double-encoded ones), strip tags, tidy spaces."""
    text = text or ""
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    text = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def norm(url: str) -> str:
    return url.split("?")[0].split("#")[0].rstrip("/")


def is_quiet(now: datetime) -> bool:
    t = now.time()
    if QUIET_START > QUIET_END:  # over midnight, e.g. 21:00–08:30
        return t >= QUIET_START or t < QUIET_END
    return QUIET_START <= t < QUIET_END


# ---------------------------------------------------------------- collecting


def get(url: str) -> requests.Response:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def from_rss(feed_url: str) -> list[dict]:
    feed = feedparser.parse(get(feed_url).content)
    items = []
    for e in feed.entries:
        if not e.get("link"):
            continue
        image = None
        for key in ("media_content", "media_thumbnail"):
            if e.get(key):
                image = e[key][0].get("url")
                break
        if not image:
            for enc in e.get("enclosures", []):
                if enc.get("type", "").startswith("image"):
                    image = enc.get("href")
                    break
        items.append({"url": e.link, "title": clean(e.get("title", "")),
                      "summary": clean(e.get("summary", "")), "image": image})
    return items


def discover_rss(home_html: str, home_url: str) -> str | None:
    soup = BeautifulSoup(home_html, "html.parser")
    link = soup.find("link", rel="alternate", type=re.compile("rss|atom"))
    return urljoin(home_url, link["href"]) if link and link.get("href") else None


def from_homepage(src: dict, home_html: str, limit: int = 15) -> list[dict]:
    """Fallback: grab article links from the homepage, read og: tags from each."""
    links, out = [], []
    for m in re.findall(src["article_re"], home_html):
        u = norm(urljoin(src["home"], m))
        if u not in links:
            links.append(u)
    for u in links[:limit]:
        try:
            out.append(read_article_meta(u))
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {u}: {exc}")
    return out


def read_article_meta(url: str) -> dict:
    soup = BeautifulSoup(get(url).text, "html.parser")

    def meta(prop):
        tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
        return tag.get("content", "").strip() if tag else ""

    return {
        "url": url,
        "title": clean(meta("og:title") or (soup.title.string if soup.title else "")),
        "summary": clean(meta("og:description") or meta("description")),
        "image": meta("og:image") or None,
    }


def collect(src: dict) -> list[dict]:
    if src.get("rss"):
        try:
            items = from_rss(src["rss"])
            if items:
                return items
        except Exception as exc:  # noqa: BLE001
            print(f"  RSS failed ({exc}), falling back to homepage")
    home_html = get(src["home"]).text
    found = discover_rss(home_html, src["home"])
    if found:
        try:
            items = from_rss(found)
            if items:
                print(f"  using discovered feed {found}")
                return items
        except Exception:  # noqa: BLE001
            pass
    return from_homepage(src, home_html)


# ---------------------------------------------------------------- claude


def ask_claude(prompt: str, max_tokens: int) -> str | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": CLAUDE_MODEL, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=90,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"]).strip()


def _json_list(text: str) -> list:
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        raise ValueError(f"no JSON list in answer: {text[:200]!r}")
    return json.loads(m.group(0))


CLASSIFY_PROMPT = """Ты редактор русскоязычного Telegram-канала «Португальские новости» для тех, кто живёт в Португалии.
Ниже список новостей с португальских сайтов (номер. [источник] заголовок — анонс).

Для КАЖДОЙ новости реши два вопроса:
1. "pt" — касается ли она Португалии: событие в Португалии; португальская политика, власти, экономика, цены, налоги, транспорт, погода, происшествия, общество, культура, спорт (португальские клубы и сборная); или решение/событие за рубежом, которое напрямую влияет на жизнь людей в Португалии (например, правила ЕС, применяемые здесь, рейсы в/из Португалии, участие Португалии или португальцев). Мировые новости без португальской связи — false, даже если о них пишут португальские СМИ.
2. "imp" — важная для аудитории канала: связана с иммиграцией и эмиграцией, AIMA (бывший SEF), видами на жительство, визами, гражданством, беженцами, легализацией иностранцев; или касается русских, украинцев, белорусов (в том числе отношений Португалии с Россией, Украиной, Беларусью). "imp" может быть true только если "pt" true.

Ответь только JSON-списком без пояснений, по одному объекту на каждую новость:
[{"i": 0, "pt": true, "imp": false}, ...]

Новости:
"""


def _fallback_flags(batch: list[tuple[str, dict]]) -> list[tuple[bool, bool]]:
    return [(True, bool(IMPORTANT_RE.search(f"{i['title']} {i['summary']}"))) for _, i in batch]


def classify(batch: list[tuple[str, dict]]) -> list[tuple[bool, bool]]:
    """Returns (concerns_portugal, important) for every (source, item)."""
    if not batch:
        return []
    if not os.getenv("ANTHROPIC_API_KEY"):
        return _fallback_flags(batch)
    flags: list[tuple[bool, bool]] = []
    for start in range(0, len(batch), 25):
        chunk = batch[start:start + 25]
        lines = "\n".join(f"{n}. [{src}] {it['title']} — {it['summary'][:300]}"
                          for n, (src, it) in enumerate(chunk))
        result = None
        for attempt in range(2):
            try:
                data = _json_list(ask_claude(CLASSIFY_PROMPT + lines, 60 + 30 * len(chunk)))
                by_i = {int(d["i"]): d for d in data if isinstance(d, dict) and "i" in d}
                result = []
                for n in range(len(chunk)):
                    d = by_i.get(n, {})
                    pt = bool(d.get("pt", False))
                    result.append((pt, pt and bool(d.get("imp", False))))
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  ! classification failed (attempt {attempt + 1}): {exc}")
        if result is None:
            print("  ! using keyword fallback for this batch (no Portugal filter)")
            result = _fallback_flags(chunk)
        flags += result
    return flags


def rewrite_with_claude(item: dict, source: str) -> str | None:
    prompt = (
        f"Перед тобой новость с португальского сайта {source}.\n"
        f"Заголовок: {item['title']}\nКратко: {item['summary']}\n\n"
        f"Напиши пост для Telegram-канала на языке: {POST_LANGUAGE}. "
        "Первая строка — короткий заголовок (без кавычек и эмодзи). "
        "Дальше 2–3 предложения своими словами, только факты из текста выше, "
        "ничего не додумывай. Без хэштегов, без ссылки, без markdown."
    )
    return ask_claude(prompt, 400)


class RewriteFailed(Exception):
    """Claude key is set but the rewrite didn't work — don't post Portuguese text."""


def build_post(item: dict, source: str, important: bool) -> tuple[str, str]:
    """Returns (post html, plain headline for the digest)."""
    text = None
    if os.getenv("ANTHROPIC_API_KEY"):
        try:
            text = rewrite_with_claude(item, source)
        except requests.HTTPError as exc:
            detail = exc.response.text[:300] if exc.response is not None else ""
            raise RewriteFailed(f"{exc} {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RewriteFailed(str(exc)) from exc
        if not text:
            raise RewriteFailed("empty answer")
    else:
        print("  ! ANTHROPIC_API_KEY is not set — posting the original Portuguese text")

    if text:
        head, _, body = text.partition("\n")
        headline = head.strip()
        post = f"<b>{html.escape(headline)}</b>\n\n{html.escape(body.strip())}"
    else:
        headline = item["title"]
        post = f"<b>{html.escape(item['title'])}</b>"
        if item["summary"]:
            post += f"\n\n{html.escape(item['summary'][:600])}"
    if important:
        post = f"{IMPORTANT_TAG}\n{post}"
    post += f"\n\nИсточник: {html.escape(source)}"
    return post, headline


# ---------------------------------------------------------------- digest


def build_digest(entries: list[dict], now: datetime) -> str:
    chosen = entries
    if len(entries) > DIGEST_MAX_ITEMS and os.getenv("ANTHROPIC_API_KEY"):
        lines = "\n".join(f"{n}. {'[ВАЖНО] ' if e.get('important') else ''}{e['title']}"
                          for n, e in enumerate(entries))
        prompt = (
            "Ниже заголовки новостей, опубликованных сегодня в канале о Португалии. "
            f"Выбери не больше {DIGEST_MAX_ITEMS} самых главных для живущих в Португалии, "
            "отмеченные [ВАЖНО] — в первую очередь. Если несколько заголовков об одном и том же, "
            "оставь один. Ответь только JSON-списком номеров по порядку значимости, например [3, 0, 7].\n\n"
            + lines
        )
        try:
            idx = [int(i) for i in _json_list(ask_claude(prompt, 200))]
            picked = [entries[i] for i in dict.fromkeys(idx) if 0 <= i < len(entries)]
            if picked:
                chosen = picked[:DIGEST_MAX_ITEMS]
        except Exception as exc:  # noqa: BLE001
            print(f"  ! digest selection failed: {exc}")
    if len(chosen) > DIGEST_MAX_ITEMS:  # no Claude: important first, then the rest
        chosen = ([e for e in chosen if e.get("important")] +
                  [e for e in chosen if not e.get("important")])[:DIGEST_MAX_ITEMS]

    out = [f"<b>Главное за {now.day} {MONTHS[now.month - 1]}</b>", ""]
    for e in chosen:
        tag = f" {IMPORTANT_TAG}" if e.get("important") else ""
        out.append(f"• {html.escape(e['title'])}{tag}")
    return "\n".join(out)


def maybe_post_digest(state: dict, now: datetime) -> None:
    if DIGEST_TIME is None or now.time() < DIGEST_TIME:
        return
    today = now.date().isoformat()
    if state["digest_date"] == today:
        return
    state["digest_date"] = today
    entries = [p for p in state["posted"] if p.get("date") == today]
    if not entries:
        print("Digest: no posts today, skipped.")
        return
    try:
        send(build_digest(entries, now), None)
        print(f"Digest posted ({len(entries)} news today).")
    except Exception as exc:  # noqa: BLE001
        print(f"  ! digest send failed: {exc}")
        state["digest_date"] = ""  # try again on the next run


# ---------------------------------------------------------------- telegram


def send(post: str, image: str | None) -> None:
    if DRY_RUN:
        print("-" * 40, "\n", post, "\n[image]", image)
        return
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHANNEL"]
    api = f"https://api.telegram.org/bot{token}"
    if image and len(post) <= 1024:
        r = requests.post(f"{api}/sendPhoto", data={"chat_id": chat, "photo": image,
                                                    "caption": post, "parse_mode": "HTML"}, timeout=TIMEOUT)
        if r.ok:
            return
        print(f"  ! sendPhoto failed ({r.text[:200]}), sending text only")
    r = requests.post(f"{api}/sendMessage", data={"chat_id": chat, "text": post, "parse_mode": "HTML",
                                                  "disable_web_page_preview": "true"}, timeout=TIMEOUT)
    r.raise_for_status()


# ---------------------------------------------------------------- queue


def trim_pending(pool: list[dict], now: datetime, cap: bool = True) -> list[dict]:
    """Drop stale entries; with cap, if too many, keep the important ones and the newest."""
    fresh = []
    for p in pool:
        try:
            age_h = (now - datetime.fromisoformat(p["added"])).total_seconds() / 3600
        except Exception:  # noqa: BLE001
            age_h = 0
        if age_h <= PENDING_MAX_HOURS:
            fresh.append(p)
    if not cap or len(fresh) <= PENDING_MAX:
        return fresh
    imp = [p for p in fresh if p["important"]]
    rest = [p for p in fresh if not p["important"]]
    if len(imp) >= PENDING_MAX:
        kept = imp[-PENDING_MAX:]
    else:
        kept = imp + rest[-(PENDING_MAX - len(imp)):]
    keep = {id(p) for p in kept}
    return [p for p in fresh if id(p) in keep]


def order_for_posting(pool: list[dict]) -> list[dict]:
    """Important first (oldest first), then the rest round-robin across sources."""
    important = [p for p in pool if p["important"]]
    by_src: dict[str, list] = {}
    for p in pool:
        if not p["important"]:
            by_src.setdefault(p["source"], []).append(p)
    rest = []
    while any(by_src.values()):
        for name in list(by_src):
            if by_src[name]:
                rest.append(by_src[name].pop(0))
    return important + rest


# ---------------------------------------------------------------- main


def main() -> None:
    seen = load_seen()
    state = load_state()
    now = datetime.now(TZ)
    first_run = not seen
    manual = bool(POST_LATEST)
    print(f"Local time: {now:%Y-%m-%d %H:%M} ({TZ.key}), quiet hours: {is_quiet(now)}")

    found: list[tuple[str, dict]] = []
    for src in SOURCES:
        print(f"[{src['name']}]")
        try:
            items = collect(src)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! failed: {exc}")
            continue
        known = set(seen.get(src["name"], []))
        new = [i for i in items if norm(i["url"]) not in known]
        print(f"  {len(items)} found, {len(new)} new")
        seen.setdefault(src["name"], []).extend(norm(i["url"]) for i in new)
        fresh = items[:POST_LATEST] if manual else new
        if KEYWORDS:
            fresh = [i for i in fresh if any(k in (i["title"] + " " + i["summary"]).lower() for k in KEYWORDS)]
        # oldest first, so the channel reads chronologically
        found += [(src["name"], i) for i in reversed(fresh)]

    if first_run and not DRY_RUN and not manual:
        # first launch: remember what's already on the sites, post nothing (no flood)
        print("First run: saved current news as already seen, nothing posted.")
        save_seen(seen)
        save_state(state, now)
        return

    candidates = []
    for (name, item), (pt, imp) in zip(found, classify(found)):
        if not pt:
            print(f"  – skipped (not about Portugal): {item['title'][:70]}")
            continue
        candidates.append({"source": name, "item": item, "important": imp, "added": now.isoformat()})
    print(f"{len(candidates)} of {len(found)} new news concern Portugal "
          f"({sum(c['important'] for c in candidates)} marked {IMPORTANT_TAG})")

    if manual:
        to_post, leftover = candidates, []
    else:
        seen_urls, pool = set(), []
        for p in state["pending"] + candidates:
            u = norm(p["item"]["url"])
            if u not in seen_urls:
                seen_urls.add(u)
                pool.append(p)
        pool = trim_pending(pool, now, cap=False)
        if is_quiet(now):
            to_post, leftover = [], pool
            print(f"Quiet hours: nothing posted, "
                  f"{len(trim_pending(pool, now))} news queued for the morning.")
        else:
            ordered = order_for_posting(pool)
            to_post, leftover = ordered[:MAX_POSTS_PER_RUN], ordered[MAX_POSTS_PER_RUN:]

    for n, p in enumerate(to_post):
        if n > 0:
            print(f"  … waiting {POST_INTERVAL_MIN:g} min before next post", flush=True)
            time.sleep(POST_INTERVAL_MIN * 60)
        current = datetime.now(TZ)
        if not manual and is_quiet(current):
            print("  Quiet hours started, the rest goes to the queue.")
            leftover = to_post[n:] + leftover
            break
        item, name = p["item"], p["source"]
        if not item.get("image"):
            # feed has no picture (e.g. CNN Portugal) — take it from the article page
            try:
                item["image"] = read_article_meta(item["url"])["image"]
            except Exception:  # noqa: BLE001
                pass
        try:
            post, headline = build_post(item, name, p["important"])
            send(post, item.get("image"))
            state["posted"].append({"date": current.date().isoformat(), "title": headline,
                                    "important": p["important"], "source": name})
            print(f"  ✓ {item['title'][:70]}", flush=True)
        except RewriteFailed as exc:
            print(f"  ! Claude rewrite failed, news kept in the queue: {exc}")
            leftover = [x for x in to_post[n:] if x is not p] + leftover
            leftover.insert(0, p)
            print("  Stopping this run, will retry next time.")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"  ! send failed: {exc}")

    if not manual:
        state["pending"] = trim_pending(leftover, datetime.now(TZ))
    maybe_post_digest(state, datetime.now(TZ))
    save_seen(seen)
    save_state(state, datetime.now(TZ))


if __name__ == "__main__":
    sys.exit(main())

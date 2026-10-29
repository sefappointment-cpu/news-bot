"""
News → Telegram channel bot.

Collects fresh articles from Portuguese news sites, optionally rewrites them
with Claude into a short post in the chosen language, and publishes them to
a Telegram channel. Designed to run on a schedule (GitHub Actions cron).

Env variables:
  TELEGRAM_BOT_TOKEN   required — token from @BotFather
  TELEGRAM_CHANNEL     required — @channel_username or numeric chat id (-100...)
  ANTHROPIC_API_KEY    optional — if set, posts are rewritten by Claude
  POST_LANGUAGE        optional — language for rewritten posts (default: Russian)
  MAX_POSTS_PER_RUN    optional — total cap per run (default: 5)
  KEYWORDS             optional — comma-separated; only matching news are posted
  DRY_RUN              optional — "1" prints posts instead of sending
"""

import html
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

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

STATE_FILE = Path(__file__).with_name("seen.json")
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN") or "5")
POST_LANGUAGE = os.getenv("POST_LANGUAGE") or "русский"
KEYWORDS = [k.strip().lower() for k in os.getenv("KEYWORDS", "").split(",") if k.strip()]
DRY_RUN = os.getenv("DRY_RUN") == "1"
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL") or "claude-haiku-5-5"

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NewsToTelegramBot/1.0)"}
TIMEOUT = 20

# ---------------------------------------------------------------- state


def load_seen() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_seen(seen: dict) -> None:
    # keep the last 500 links per source so the file doesn't grow forever
    trimmed = {k: v[-500:] for k, v in seen.items()}
    STATE_FILE.write_text(json.dumps(trimmed, ensure_ascii=False, indent=1), encoding="utf-8")


def norm(url: str) -> str:
    return url.split("?")[0].split("#")[0].rstrip("/")


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
        summary = BeautifulSoup(e.get("summary", ""), "html.parser").get_text(" ", strip=True)
        items.append({"url": e.link, "title": e.get("title", ""), "summary": summary, "image": image})
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
        "title": meta("og:title") or (soup.title.string.strip() if soup.title else ""),
        "summary": meta("og:description") or meta("description"),
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


# ---------------------------------------------------------------- writing


def rewrite_with_claude(item: dict, source: str) -> str | None:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    prompt = (
        f"Перед тобой новость с португальского сайта {source}.\n"
        f"Заголовок: {item['title']}\nКратко: {item['summary']}\n\n"
        f"Напиши пост для Telegram-канала на языке: {POST_LANGUAGE}. "
        "Первая строка — короткий заголовок (без кавычек и эмодзи). "
        "Дальше 2–3 предложения своими словами, только факты из текста выше, "
        "ничего не додумывай. Без хэштегов, без ссылки, без markdown."
    )
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        json={"model": CLAUDE_MODEL, "max_tokens": 400, "messages": [{"role": "user", "content": prompt}]},
        timeout=60,
    )
    r.raise_for_status()
    return "".join(b.get("text", "") for b in r.json()["content"]).strip()


def build_post(item: dict, source: str) -> str:
    text = None
    try:
        text = rewrite_with_claude(item, source)
    except Exception as exc:  # noqa: BLE001
        print(f"  ! Claude rewrite failed: {exc}")

    if text:
        head, _, body = text.partition("\n")
        post = f"<b>{html.escape(head.strip())}</b>\n\n{html.escape(body.strip())}"
    else:
        post = f"<b>{html.escape(item['title'])}</b>"
        if item["summary"]:
            post += f"\n\n{html.escape(item['summary'][:600])}"
    post += f"\n\nИсточник: {html.escape(source)}"
    return post


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


# ---------------------------------------------------------------- main


def main() -> None:
    seen = load_seen()
    first_run = not seen
    queue = []

    for src in SOURCES:
        print(f"[{src['name']}]")
        try:
            items = collect(src)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! failed: {exc}")
            continue
        known = set(seen.get(src["name"], []))
        fresh = [i for i in items if norm(i["url"]) not in known]
        print(f"  {len(items)} found, {len(fresh)} new")
        seen.setdefault(src["name"], []).extend(norm(i["url"]) for i in fresh)
        if KEYWORDS:
            fresh = [i for i in fresh if any(k in (i["title"] + " " + i["summary"]).lower() for k in KEYWORDS)]
        # oldest first, so the channel reads chronologically
        queue += [(src["name"], i) for i in reversed(fresh)]

    if first_run and not DRY_RUN:
        # first launch: remember what's already on the sites, post nothing (no flood)
        print("First run: saved current news as already seen, nothing posted.")
        save_seen(seen)
        return

    # round-robin across sources so one site doesn't take all slots
    by_src: dict[str, list] = {}
    for name, item in queue:
        by_src.setdefault(name, []).append(item)
    ordered = []
    while any(by_src.values()) and len(ordered) < MAX_POSTS_PER_RUN:
        for name in list(by_src):
            if by_src[name] and len(ordered) < MAX_POSTS_PER_RUN:
                ordered.append((name, by_src[name].pop(0)))

    for name, item in ordered:
        try:
            send(build_post(item, name), item.get("image"))
            print(f"  ✓ {item['title'][:70]}")
            time.sleep(3)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! send failed: {exc}")

    save_seen(seen)


if __name__ == "__main__":
    sys.exit(main())

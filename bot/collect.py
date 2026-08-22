"""Збір сирого матеріалу: публічні ТГ-канали через t.me/s/ і RSS.

Головне правило модуля: одне впале джерело не валить прогін. Мережа падає
постійно, фіди мігрують — це нормальний режим роботи, а не виняток.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

import feedparser
import requests
from bs4 import BeautifulSoup

from bot.config import USER_AGENT
from bot.state import mark_source_failed, mark_source_ok, now, source_is_benched

log = logging.getLogger(__name__)

TG_LIMIT_PER_CHANNEL = 12
RSS_LIMIT_PER_FEED = 15
MAX_AGE_HOURS = 36  # старіше просто не новина


def make_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "uk,en;q=0.8",
    })
    return session


_LETTERS_RE = re.compile(r"[^\W\d_]", re.UNICODE)


def headline(text: str, min_letters: int = 20) -> str:
    """Перший ЗМІСТОВНИЙ рядок як заголовок.

    Пости в ТГ регулярно починаються з самотньої емодзі, хештега або «⚡️UPD».
    Якщо брати буквально перший рядок, заголовком стане «❗️» — і тоді два
    непов'язані пости з однаковою емодзі дадуть однаковий simhash, а другий
    мовчки зникне як дубль. Тому шукаємо перший рядок, у якому справді є слова.
    """
    for line in text.split("\n"):
        line = line.strip()
        if line.startswith("#"):
            continue
        if len(_LETTERS_RE.findall(line)) >= min_letters:
            return line[:300]

    # Нічого путнього по рядках — беремо суцільний текст як є.
    flat = " ".join(part.strip() for part in text.split("\n") if part.strip())
    return flat[:300]


def _fresh_enough(published: datetime | None) -> bool:
    if published is None:
        return True  # немає дати — не привід викидати, вирішить дедуп
    return now() - published < timedelta(hours=MAX_AGE_HOURS)


_TINY_IMAGE_RE = re.compile(r"/(1x1|pixel|spacer|blank)\.|width=1&|/badge/", re.I)

# Заглушки, які видання підставляють, коли ілюстрації до статті немає.
# Взяти їх — гірше, ніж не брати нічого: у каналі буде однакова картинка
# під різними новинами. «Суспільне», наприклад, віддає images/default.jpg.
_PLACEHOLDER_RE = re.compile(
    r"/(default|placeholder|no[-_]?image|noimage|share|og[-_]image|logo|stub)"
    r"[-_.\d]*\.(jpe?g|png|webp)$",
    re.I,
)


def _looks_like_photo(url: str) -> bool:
    """Відсіяти лічильники, заглушки й те, чого Telegram не візьме."""
    if not url or not url.startswith("http"):
        return False
    if _TINY_IMAGE_RE.search(url) or _PLACEHOLDER_RE.search(url.split("?")[0]):
        return False
    # Власний CDN Telegram закритий для сторонніх завантажень: sendPhoto по
    # такому URL стабільно віддає «failed to get HTTP URL content». Тому фото
    # з ТГ-каналів не використовуємо взагалі — перевірено на живих посиланнях.
    if "telesco.pe" in url or "cdn-telegram.org" in url:
        return False
    # SVG Telegram як фото не приймає.
    return not url.lower().split("?")[0].endswith(".svg")


def _entry_image(entry, summary_html: str) -> str:
    """Знайти ілюстрацію до запису RSS.

    Видання кладуть її в чотири різні місця, і жодне не є стандартом де-факто,
    тому перебираємо по черзі. Малі картинки відсіюємо: Telegram однаково
    відмовиться слати трекінговий піксель, а пост через це втратить фото.
    """
    for media in (entry.get("media_content") or []) + (entry.get("media_thumbnail") or []):
        url = media.get("url", "")
        try:
            if int(media.get("width") or 0) and int(media["width"]) < 200:
                continue
        except (TypeError, ValueError):
            pass
        if _looks_like_photo(url):
            return url

    for enc in entry.get("enclosures") or []:
        if str(enc.get("type", "")).startswith("image/") and _looks_like_photo(enc.get("href", "")):
            return enc["href"]

    for link in entry.get("links") or []:
        if str(link.get("type", "")).startswith("image/") and _looks_like_photo(link.get("href", "")):
            return link["href"]

    if "<img" in summary_html:
        img = BeautifulSoup(summary_html, "html.parser").find("img")
        if img and _looks_like_photo(img.get("src", "")):
            return img["src"]

    return ""


def _entry_datetime(entry) -> datetime | None:
    for field in ("published_parsed", "updated_parsed"):
        parsed = entry.get(field)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def fetch_telegram(session: requests.Session, source: dict) -> list[dict]:
    """Публічне вебпрев'ю каналу. Логін не потрібен, апдейти Telegram не читаються."""
    channel = source["value"]
    url = f"https://t.me/s/{channel}"

    resp = session.get(url, timeout=20)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    blocks = soup.select("div.tgme_widget_message")
    if not blocks:
        raise ValueError(
            "сторінка без повідомлень — канал приватний або без вебпрев'ю"
        )

    items: list[dict] = []
    for block in blocks[-TG_LIMIT_PER_CHANNEL:]:
        text_node = block.select_one("div.tgme_widget_message_text")
        if not text_node:
            continue

        text = text_node.get_text("\n", strip=True)
        if len(text) < 40:
            continue  # реакція, стікер, «підписуйтесь» — не новина

        time_node = block.select_one("time[datetime]")
        published = None
        if time_node:
            try:
                published = datetime.fromisoformat(
                    time_node["datetime"].replace("Z", "+00:00")
                )
            except (ValueError, KeyError):
                published = None
        if not _fresh_enough(published):
            continue

        # Зовнішнє посилання з поста — кандидат на першоджерело.
        # Внутрішні t.me відкидаємо: лінк на посередника заборонений документом 01.
        outbound = ""
        for a in text_node.select("a[href]"):
            href = a.get("href", "")
            if href.startswith("http") and "t.me/" not in href:
                outbound = href
                break

        photo = ""
        wrap = block.select_one(".tgme_widget_message_photo_wrap")
        if wrap:
            style = wrap.get("style", "")
            match = re.search(r"background-image:\s*url\('([^']+)'\)", style)
            if match and _looks_like_photo(match.group(1)):
                photo = match.group(1)

        post_id = block.get("data-post", f"{channel}/?")
        items.append({
            "title": headline(text),
            "text": text[:4000],
            "url": outbound,
            "origin": f"https://t.me/{post_id}",
            "rubric": source["rubric"],
            "weight": source["weight"],
            "image": photo,
            "source_type": "tg",
            "source": channel,
            "published_at": published.isoformat() if published else "",
        })

    return items


def fetch_rss(session: requests.Session, source: dict) -> list[dict]:
    """RSS/Atom. Завантажуємо самі, щоб контролювати таймаут —
    feedparser з URL таймаута не має."""
    url = source["value"]

    resp = session.get(url, timeout=15)
    resp.raise_for_status()

    parsed = feedparser.parse(resp.content)
    if not parsed.entries:
        raise ValueError(f"фід без записів (bozo={parsed.bozo})")

    items: list[dict] = []
    for entry in parsed.entries[:RSS_LIMIT_PER_FEED]:
        title = (entry.get("title") or "").strip()
        if not title:
            continue

        published = _entry_datetime(entry)
        if not _fresh_enough(published):
            continue

        raw_summary = (entry.get("summary") or entry.get("description") or "").strip()
        summary = raw_summary
        if "<" in summary:
            # Проганяємо через парсер лише те, де справді є теги. Інакше
            # BeautifulSoup сипле MarkupResemblesLocatorWarning на короткі
            # описи, схожі на шлях чи URL, і засмічує лог прогону.
            summary = BeautifulSoup(summary, "html.parser").get_text(" ", strip=True)

        link = entry.get("link") or ""
        items.append({
            "title": title[:300],
            "text": f"{title}\n\n{summary}"[:4000],
            "url": link,
            "origin": link,
            "rubric": source["rubric"],
            "weight": source["weight"],
            "image": _entry_image(entry, raw_summary),
            "source_type": "rss",
            "source": parsed.feed.get("title", url)[:80],
            "published_at": published.isoformat() if published else "",
        })

    return items


def collect_all(sources: list[dict], state: dict) -> list[dict]:
    """Обійти всі джерела. Кожне падіння — warning і далі, ніколи не виняток назовні."""
    session = make_session()
    items: list[dict] = []
    ok_count = failed = benched = 0

    for source in sources:
        key = source["value"]

        if source_is_benched(state, key):
            benched += 1
            log.warning(
                "джерело на лаві запасних після %d невдач: %s",
                state["dead_sources"][key]["fails"], key,
            )
            continue

        try:
            fetched = (
                fetch_telegram(session, source)
                if source["kind"] == "tg"
                else fetch_rss(session, source)
            )
        except Exception as exc:
            # Свідомо широкий except: сюди прилітає все — від DNS до кривого XML.
            # Жоден із цих випадків не є приводом зупинити прогін.
            failed += 1
            mark_source_failed(state, key)
            log.warning("джерело впало (%s): %s — %s",
                        source["kind"], key, type(exc).__name__ + ": " + str(exc)[:120])
            continue

        mark_source_ok(state, key)
        ok_count += 1
        items.extend(fetched)
        log.info("  %-4s %-10s %2d айтемів  %s",
                 source["kind"], source["rubric"], len(fetched), key)

    # Прибрати з dead_sources те, чого вже немає в конфігу. Інакше запис про
    # видалене джерело висить вічно (він чиститься лише при успішному зборі)
    # і збиває з пантелику при розборі: здається, що джерело досі падає.
    current = {s["value"] for s in sources}
    stale = [url for url in state["dead_sources"] if url not in current]
    for url in stale:
        del state["dead_sources"][url]
    if stale:
        log.info("прибрано %d записів про джерела, яких уже немає в конфігу", len(stale))

    log.info(
        "зібрано %d айтемів із %d джерел (впало %d, на лаві %d)",
        len(items), ok_count, failed, benched,
    )
    return items

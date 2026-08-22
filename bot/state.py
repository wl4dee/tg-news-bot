"""state.json: історія прогонів. Читання, запис, очищення."""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone

from bot.config import PUBLISHED_TTL_DAYS, SEEN_TTL_HOURS

log = logging.getLogger(__name__)

STATE_PATH = os.environ.get("STATE_PATH", "state.json")

EMPTY: dict = {
    "seen": [],
    "published": [],
    "stats": [],
    "dead_sources": {},
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime | None:
    """Толерантний парсер: у стан могли потрапити дати з різних джерел."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def load(path: str = STATE_PATH) -> dict:
    if not os.path.exists(path):
        log.info("state.json не знайдено, починаємо з чистого стану")
        return json.loads(json.dumps(EMPTY))
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        # Зіпсований стан не має валити прогін: гірше втратити прогін, ніж історію.
        log.error("state.json не читається (%s), починаємо з чистого стану", exc)
        return json.loads(json.dumps(EMPTY))

    for key, value in EMPTY.items():
        data.setdefault(key, json.loads(json.dumps(value)))
    return data


def save(state: dict, path: str = STATE_PATH) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    os.replace(tmp, path)
    log.info(
        "state.json записано: seen=%d published=%d stats=%d dead=%d",
        len(state["seen"]), len(state["published"]),
        len(state["stats"]), len(state["dead_sources"]),
    )


def prune(state: dict) -> None:
    """seen — 48 год, published — 14 днів. stats не чиститься НІКОЛИ:
    це дані для калібровки оцінювача, вони цінні саме накопиченням."""
    seen_cutoff = now() - timedelta(hours=SEEN_TTL_HOURS)
    pub_cutoff = now() - timedelta(days=PUBLISHED_TTL_DAYS)

    before_seen = len(state["seen"])
    state["seen"] = [
        r for r in state["seen"]
        if (ts := parse_iso(r.get("ts", ""))) and ts > seen_cutoff
    ]

    before_pub = len(state["published"])
    state["published"] = [
        r for r in state["published"]
        if (ts := parse_iso(r.get("ts", ""))) and ts > pub_cutoff
    ]

    log.info(
        "очищення: seen %d→%d, published %d→%d, stats %d (не чиститься)",
        before_seen, len(state["seen"]),
        before_pub, len(state["published"]),
        len(state["stats"]),
    )


def recent_published(state: dict, limit: int = 30) -> list[dict]:
    """Останні пости для контексту моделі — щоб вона зібрала блок «Раніше:»."""
    items = sorted(
        state["published"],
        key=lambda r: parse_iso(r.get("ts", "")) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return [
        {
            "message_id": r.get("message_id"),
            "story_key": r.get("story_key", ""),
            "topic": r.get("topic", ""),
            "ts": r.get("ts", ""),
        }
        for r in items[:limit]
    ]


def mark_source_failed(state: dict, url: str) -> None:
    entry = state["dead_sources"].setdefault(url, {"fails": 0, "last": ""})
    entry["fails"] = int(entry.get("fails", 0)) + 1
    entry["last"] = now_iso()


def mark_source_ok(state: dict, url: str) -> None:
    if url in state["dead_sources"]:
        del state["dead_sources"][url]


def source_is_benched(state: dict, url: str, fail_threshold: int = 5,
                      cooldown_hours: int = 6) -> bool:
    """М'який бекоф замість вічного бану: фіди мігрують і воскресають."""
    entry = state["dead_sources"].get(url)
    if not entry or int(entry.get("fails", 0)) < fail_threshold:
        return False
    last = parse_iso(entry.get("last", ""))
    if not last:
        return False
    return now() - last < timedelta(hours=cooldown_hours)

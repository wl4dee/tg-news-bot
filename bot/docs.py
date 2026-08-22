"""Google Docs як конфіг. Промпт і джерела читаються при кожному запуску."""
from __future__ import annotations

import logging
import re

import requests

from bot.config import (
    FALLBACK_CANDIDATE_LIMIT,
    FALLBACK_TOTAL_LIMIT,
    USER_AGENT,
    ConfigError,
)

log = logging.getLogger(__name__)

# Ознаки того, що Google віддав сторінку логіну замість тексту документа.
#
# Навмисно НЕ шукаємо «<html» у тілі: документ 01 — це документ ПРО HTML-розмітку
# Telegram, у ньому «<html>» цілком може стояти як приклад, і збирач відмовлявся б
# стартувати на цілком робочому документі. Надійна ознака — Content-Type:
# /export?format=txt віддає text/plain, а редирект на логін — text/html.
_LOGIN_MARKERS = ("accounts.google.com/servicelogin", "accounts.google.com/v3/signin")

# Google Docs при експорті в txt екранує службові символи зворотним слешем.
# chr(92) — це і є той слеш; так регулярка не залежить від екранування в шелі.
_UNESCAPE = re.compile(chr(92) * 2 + r"([#_*&<>.\[\]-])")


def fetch(url: str, label: str) -> str:
    """Завантажити документ як текст. Редиректи Google слідуємо обов'язково."""
    try:
        resp = requests.get(
            url,
            timeout=20,
            allow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException as exc:
        raise ConfigError(
            f"Документ «{label}» не завантажився: {exc}.\n"
            f"Перевір мережу і сам URL (має закінчуватись на /export?format=txt)."
        ) from exc

    if resp.status_code != 200:
        raise ConfigError(
            f"Документ «{label}» повернув HTTP {resp.status_code}.\n"
            f"Найімовірніша причина: доступ за посиланням не відкрито.\n"
            f"Відкрий документ → Поділитися → Доступ за посиланням → Читач.\n"
            f"Це пункт 7 чекліста запуску."
        )

    content_type = resp.headers.get("Content-Type", "").lower()

    # Декодуємо самі, а не через resp.text. Для text/* без явного charset
    # requests за RFC падає на ISO-8859-1, і тоді «Україна» перетворюється на
    # «Ð£ÐºÑÐ°ÑÐ½Ð°». Назви рубрик приходять саме звідси й далі зіставляються
    # з лімітами — зламана кодування тут ламає баланс рубрик мовчки.
    text = resp.content.decode("utf-8", errors="replace")

    if "text/html" in content_type or any(
        marker in resp.url.lower() for marker in _LOGIN_MARKERS
    ):
        raise ConfigError(
            f"Документ «{label}» віддав HTML замість тексту (Content-Type: "
            f"{content_type or 'невідомий'}) — це сторінка логіну Google.\n"
            f"Доступ за посиланням не відкрито: Поділитися → Доступ за посиланням → Читач.\n"
            f"Також переконайся, що URL має вигляд .../export?format=txt"
        )

    if not text.strip():
        raise ConfigError(f"Документ «{label}» порожній.")

    log.info("документ «%s» завантажено, %d символів", label, len(text))
    return text


def _clean(cell: str) -> str:
    r"""Google Docs при експорті в txt екранує символи зворотним слешем
    (`\#`, `\_`, `\-`) і підставляє нерозривні пробіли. Знімаємо це."""
    cell = cell.replace("\u00a0", " ").replace("\ufeff", "")
    cell = _UNESCAPE.sub(r"\1", cell)
    return cell.strip()


def parse_sources(text: str) -> dict:
    """Розібрати документ 02.

    Формат рядка:  тип | значення | рубрика | вага
    Ліміти:        limit | рубрика | N
    Псевдорубрики: _total (усього за прогін), _candidates (скільки йде в Gemini)
    Рядки з # — коментарі.
    """
    sources: list[dict] = []
    limits: dict[str, int] = {}
    skipped = 0

    for raw_line in text.splitlines():
        line = _clean(raw_line)
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            continue

        parts = [_clean(p) for p in line.split("|")]
        kind = parts[0].lower()

        if kind == "limit":
            if len(parts) < 3:
                skipped += 1
                continue
            rubric = parts[1]
            try:
                limits[rubric] = int(re.sub(r"\D", "", parts[2]) or 0)
            except ValueError:
                skipped += 1
            continue

        if kind not in ("tg", "rss"):
            # tg_private і решта майбутніх типів — тихо пропускаємо, вони ще не наші.
            skipped += 1
            continue

        if len(parts) < 3:
            skipped += 1
            continue

        value = parts[1]
        rubric = parts[2]
        try:
            weight = int(re.sub(r"\D", "", parts[3])) if len(parts) > 3 else 2
        except ValueError:
            weight = 2
        weight = max(1, min(3, weight))

        if kind == "tg":
            value = value.lstrip("@").strip()
            if not value or value.startswith("+"):
                # Приватні інвайт-лінки вебпрев'ю не мають — потрібен Telethon, це v2.
                skipped += 1
                continue

        sources.append({
            "kind": kind,
            "value": value,
            "rubric": rubric,
            "weight": weight,
            "id": value,
        })

    if not sources:
        raise ConfigError(
            "У документі джерел не знайдено жодного робочого рядка.\n"
            "Очікуваний формат: `rss | https://... | крипта | 2`\n"
            "Перевір, що документ експортується як текст, а не як HTML."
        )

    total = limits.get("_total", FALLBACK_TOTAL_LIMIT)
    candidates = limits.get("_candidates", FALLBACK_CANDIDATE_LIMIT)
    rubric_limits = {k: v for k, v in limits.items() if not k.startswith("_")}

    log.info(
        "джерела: %d (tg=%d, rss=%d), рубрик з лімітами: %d, "
        "усього за прогін: %d, кандидатів у модель: %d, пропущено рядків: %d",
        len(sources),
        sum(1 for s in sources if s["kind"] == "tg"),
        sum(1 for s in sources if s["kind"] == "rss"),
        len(rubric_limits), total, candidates, skipped,
    )

    return {
        "sources": sources,
        "rubric_limits": rubric_limits,
        "total_limit": total,
        "candidate_limit": candidates,
    }


if __name__ == "__main__":
    # Локальна перевірка парсера на файлі: python -m bot.docs sources.txt
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    with open(sys.argv[1], encoding="utf-8") as fh:
        cfg = parse_sources(fh.read())
    for s in cfg["sources"]:
        print(f"  {s['kind']:4} w{s['weight']} {s['rubric']:10} {s['value']}")
    print("\nліміти рубрик:", cfg["rubric_limits"])
    print("усього за прогін:", cfg["total_limit"], "| кандидатів:", cfg["candidate_limit"])

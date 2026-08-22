"""Виклик Gemini і розбір JSON-контракту.

Контракт описаний у .claude/rules/prompt-contract.md і в документі 01.
Міняєш тут — міняй і там, обидва місця.

Головне правило модуля: зіпсована відповідь моделі НІКОЛИ не валить прогін.
Один кривий JSON із дванадцяти айтемів — це лог і `continue`, не виняток.
"""
from __future__ import annotations

import json
import logging
import re
import time

import requests

from bot.config import GEMINI_MODEL, GEMINI_PAUSE_SEC, env

log = logging.getLogger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

MAX_ATTEMPTS = 3
TEXT_LIMIT = 4096  # ліміт Telegram на довжину повідомлення

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

REQUIRED_ON_PUBLISH = ("score", "story_key", "rubric", "text", "source_url")


def build_prompt(prompt_doc: str, item: dict, recent: list[dict],
                 is_retry: bool = False) -> str:
    """Промпт = документ 01 як є + сирий айтем + контекст останніх постів.

    RECENT_POSTS потрібен рівно для одного: щоб модель зібрала блок «Раніше:»
    і не переказала новину, яка вже виходила. Це головна фішка каналу —
    не викидай цей блок заради економії токенів.
    """
    raw_item = {
        "rubric": item.get("rubric", ""),
        "weight": item.get("weight", 2),
        "source_type": item.get("source_type", ""),
        "source": item.get("source", ""),
        "title": item.get("title", ""),
        "text": item.get("text", ""),
        "url": item.get("url", "") or item.get("origin", ""),
        "published_at": item.get("published_at", ""),
    }

    parts = [
        prompt_doc,
        "",
        "════════════════════════════════",
        "RECENT_POSTS — останні пости каналу (для блоку «Раніше:»)",
        "════════════════════════════════",
        json.dumps(recent, ensure_ascii=False, indent=1),
        "",
        "════════════════════════════════",
        "RAW_ITEM — новина, яку треба обробити",
        "════════════════════════════════",
        json.dumps(raw_item, ensure_ascii=False, indent=1),
    ]

    if is_retry:
        parts += [
            "",
            "УВАГА: попередній варіант цього поста редактор відхилив і попросив "
            "переписати. Зроби інакше — інший ракурс, інша структура, інший лід. "
            "Не повторюй попереднє формулювання.",
        ]

    parts += [
        "",
        "Поверни ТІЛЬКИ JSON за форматом з розділу «ФОРМАТ ВІДПОВІДІ». "
        "Без ``` і без пояснень.",
    ]
    return "\n".join(parts)


def _call_api(api_key: str, prompt: str) -> str | None:
    """Один запит із ретраями на 429 і 5xx. Повертає сирий текст або None."""
    url = ENDPOINT.format(model=GEMINI_MODEL)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 2048,
            "response_mime_type": "application/json",
        },
    }

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                url,
                params={"key": api_key},
                json=payload,
                timeout=60,
                headers={"Content-Type": "application/json"},
            )
        except requests.RequestException as exc:
            log.warning("Gemini: мережа впала (спроба %d/%d): %s",
                        attempt, MAX_ATTEMPTS, exc)
            time.sleep(2 ** attempt)
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except (ValueError, KeyError, IndexError):
                # Порожній кандидат буває при спрацюванні фільтрів безпеки.
                reason = ""
                try:
                    reason = str(resp.json().get("promptFeedback", ""))[:200]
                except ValueError:
                    pass
                log.error("Gemini: відповідь без тексту. promptFeedback=%s", reason)
                return None

        if resp.status_code == 429 or resp.status_code >= 500:
            wait = 2 ** attempt * 5
            log.warning("Gemini: HTTP %s, чекаємо %d с (спроба %d/%d)",
                        resp.status_code, wait, attempt, MAX_ATTEMPTS)
            time.sleep(wait)
            continue

        # 400/403 — це не тимчасове: кривий ключ, вимкнений API, неправильна модель.
        log.error("Gemini: HTTP %s — %s", resp.status_code, resp.text[:300])
        return None

    log.error("Gemini: вичерпано %d спроб", MAX_ATTEMPTS)
    return None


def parse_response(raw: str) -> dict | None:
    """Розібрати відповідь моделі. Будь-яка проблема → лог і None, ніколи виняток."""
    if not raw or not raw.strip():
        log.error("модель повернула порожню відповідь")
        return None

    cleaned = _FENCE_RE.sub("", raw.strip())
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.error("JSON не розібрався (%s). Сирий вивід: %s", exc, raw[:500])
        return None

    if not isinstance(data, dict):
        log.error("модель повернула не об'єкт, а %s. Сирий вивід: %s",
                  type(data).__name__, raw[:500])
        return None

    if not data.get("publish"):
        log.info("модель відмовила: %s", str(data.get("reason", "без причини"))[:200])
        return {"publish": False, "reason": data.get("reason", "")}

    missing = [f for f in REQUIRED_ON_PUBLISH if not data.get(f)]
    if missing:
        log.error("у відповіді немає обов'язкових полів: %s. Сирий вивід: %s",
                  ", ".join(missing), raw[:500])
        return None

    try:
        score = int(data["score"])
    except (TypeError, ValueError):
        log.error("score не число: %r", data.get("score"))
        return None
    # Затискаємо, а не відкидаємо: вихід за межі — дрібниця проти втрати поста.
    data["score"] = max(1, min(10, score))

    text = str(data["text"])
    if len(text) > TEXT_LIMIT:
        # Не обрізаємо: обрізаний HTML — це зламані entities і 400 від Telegram.
        log.error("текст %d символів, ліміт Telegram %d — айтем відкинуто",
                  len(text), TEXT_LIMIT)
        return None
    data["text"] = text

    ids = data.get("earlier_ids") or []
    data["earlier_ids"] = [int(i) for i in ids if str(i).lstrip("-").isdigit()][:4]
    data["template"] = str(data.get("template", ""))[:4]

    return data


def generate(prompt_doc: str, items: list[dict], recent: list[dict]) -> list[dict]:
    """Один виклик моделі на айтем, із паузою під безкоштовний тариф (≈10 RPM)."""
    api_key = env("GEMINI_API_KEY")
    results: list[dict] = []
    refused = broken = 0

    for index, item in enumerate(items, 1):
        if index > 1:
            time.sleep(GEMINI_PAUSE_SEC)

        log.info("[%d/%d] %s | %s", index, len(items),
                 item.get("rubric", "?"), item.get("title", "")[:70])

        prompt = build_prompt(prompt_doc, item, recent, item.get("is_retry", False))
        raw = _call_api(api_key, prompt)
        if raw is None:
            broken += 1
            continue

        parsed = parse_response(raw)
        if parsed is None:
            broken += 1
            continue
        if not parsed.get("publish"):
            refused += 1
            continue

        parsed["_item"] = item
        results.append(parsed)
        log.info("    → score %s, рубрика %s, шаблон %s",
                 parsed["score"], parsed["rubric"], parsed.get("template", "-"))

    log.info(
        "модель: %d айтемів → %d готових постів (відмов: %d, зіпсованих відповідей: %d)",
        len(items), len(results), refused, broken,
    )
    return results

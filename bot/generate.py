"""Виклик Gemini і розбір JSON-контракту.

Контракт описаний у .claude/rules/prompt-contract.md і в config/prompt.md.
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

from bot.config import (
    GEMINI_MODEL,
    GEMINI_PAUSE_SEC,
    GEMINI_THINKING_BUDGET,
    env,
)

log = logging.getLogger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

MAX_ATTEMPTS = 3
TEXT_LIMIT = 4096  # ліміт Telegram на довжину повідомлення
MAX_OUTPUT_TOKENS = 4096

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)

# Рубрику сюди свідомо НЕ включено: вона вже відома з config/sources.txt,
# бо задана поруч із джерелом. Якщо модель її поверне — приймаємо (буває, що
# новина з крипто-джерела насправді про ринки), якщо ні — беремо з айтема.
# Вимагати її від моделі означало б відкидати цілком нормальні пости.
REQUIRED_ON_PUBLISH = ("score", "story_key", "text", "source_url")


def build_prompt(prompt_doc: str, item: dict, recent: list[dict],
                 is_retry: bool = False) -> str:
    """Промпт = config/prompt.md як є + сирий айтем + контекст останніх постів.

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


# Окремий маркер, а не None: None означає «цей айтем не вийшов, беремо наступний»,
# а тут іти далі безглуздо — квота скінчилась для всього прогону.
DAILY_QUOTA_EXHAUSTED = "\x00DAILY_QUOTA"


def _daily_quota_limit(resp) -> str | None:
    """Якщо 429 — саме про денну квоту, повернути її значення рядком.

    Google кладе його в details[].violations[].quotaId виду
    «GenerateRequestsPerDayPerProjectPerModel-FreeTier». Дістаємо звідти ще й
    сам ліміт: інакше його ніяк не дізнатись, крім як упертись у нього.
    """
    try:
        details = resp.json().get("error", {}).get("details", [])
    except ValueError:
        return None
    for detail in details:
        for violation in detail.get("violations", []) or []:
            if "PerDay" in str(violation.get("quotaId", "")):
                return str(violation.get("quotaValue", "невідомо"))
    return None


def _call_api(api_key: str, prompt: str) -> str | None:
    """Один запит із ретраями на 429 і 5xx. Повертає сирий текст або None."""
    url = ENDPOINT.format(model=GEMINI_MODEL)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "response_mime_type": "application/json",
        },
    }
    # Gemini 2.5 Flash — модель із мисленням, і воно ввімкнене за замовчуванням.
    # Токени мислення рахуються в maxOutputTokens, тож модель здатна витратити
    # весь ліміт на роздуми й повернути ПОРОЖНЮ відповідь із finishReason=
    # MAX_TOKENS — зовні це виглядає як «модель нічого не пропускає». Для
    # переписування новини за готовим шаблоном мислення не потрібне.
    if GEMINI_THINKING_BUDGET >= 0:
        payload["generationConfig"]["thinkingConfig"] = {
            "thinkingBudget": GEMINI_THINKING_BUDGET
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
            except ValueError:
                log.error("Gemini: 200, але тіло не JSON")
                return None
            try:
                return data["candidates"][0]["content"]["parts"][0]["text"]
            except (KeyError, IndexError):
                # Порожній кандидат. Причина майже завжди в одному з двох:
                # фільтри безпеки (finishReason=SAFETY) або вичерпаний
                # maxOutputTokens (MAX_TOKENS) — друге трапляється, коли модель
                # витратила ліміт на мислення. Друкуємо і те, і те: без цих
                # полів симптом виглядає просто як «модель нічого не пропускає».
                cand = (data.get("candidates") or [{}])[0]
                log.error(
                    "Gemini: відповідь без тексту. finishReason=%s, "
                    "promptFeedback=%s, usage=%s",
                    cand.get("finishReason", "?"),
                    str(data.get("promptFeedback", ""))[:120],
                    str(data.get("usageMetadata", ""))[:200],
                )
                return None

        if resp.status_code == 429:
            # Денна і хвилинна квоти — різні речі. Хвилинну можна перечекати,
            # денну — ні: до півночі за тихоокеанським часом нічого не зміниться.
            # Без цієї гілки прогін витрачав по 70 с на айтем на марні ретраї.
            limit = _daily_quota_limit(resp)
            if limit is not None:
                log.error(
                    "Gemini: вичерпано ДЕННУ квоту безкоштовного тарифу "
                    "(%s запитів на добу для моделі %s). Генерацію зупинено — "
                    "ретраї тут не допоможуть. Або зменш _candidates у "
                    "config/sources.txt і частоту крону, або візьми іншу модель "
                    "через GEMINI_MODEL.", limit, GEMINI_MODEL,
                )
                return DAILY_QUOTA_EXHAUSTED
            wait = 2 ** attempt * 5
            log.warning("Gemini: 429 (хвилинний ліміт), чекаємо %d с (спроба %d/%d)",
                        wait, attempt, MAX_ATTEMPTS)
            time.sleep(wait)
            continue

        if resp.status_code >= 500:
            wait = 2 ** attempt * 5
            log.warning("Gemini: HTTP %s, чекаємо %d с (спроба %d/%d)",
                        resp.status_code, wait, attempt, MAX_ATTEMPTS)
            time.sleep(wait)
            continue

        # Не всі моделі приймають thinkingBudget=0: Flash приймає, а, скажімо,
        # gemini-3.6-flash відповідає на це 400 «invalid argument». Один раз
        # пробуємо те саме без поля мислення — інакше зміна моделі мовчки
        # вбиває весь прогін, а виглядає це як «модель нічого не пропускає».
        if (resp.status_code == 400
                and "thinkingConfig" in payload["generationConfig"]):
            log.warning(
                "Gemini: модель %s не приймає thinkingConfig — повторюю без нього",
                GEMINI_MODEL,
            )
            payload["generationConfig"].pop("thinkingConfig")
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
        # Позначку ставимо ДО виклику: якщо модель відмовила чи зіпсувала JSON,
        # повторювати той самий айтем наступного прогону немає сенсу. А от
        # айтеми після зупинки по квоті лишаються непозначеними й повернуться.
        item["_processed"] = True

        prompt = build_prompt(prompt_doc, item, recent, item.get("is_retry", False))
        raw = _call_api(api_key, prompt)

        if raw is DAILY_QUOTA_EXHAUSTED:
            # Решту айтемів навіть не пробуємо: вони підуть у наступний прогін,
            # бо в seen[] потрапляють ті самі кандидати, а не оброблені.
            log.warning("оброблено %d із %d айтемів до вичерпання денної квоти",
                        index - 1, len(items))
            break

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

        # Рубрика з джерела — основна; модель може її перекрити, якщо новина
        # насправді про інше. Порожню або невідому рубрику не беремо: за нею
        # рахуються ліміти, і чужа назва обійшла б баланс рубрик.
        if not parsed.get("rubric"):
            parsed["rubric"] = item.get("rubric", "")

        parsed["_item"] = item
        results.append(parsed)
        log.info("    → score %s, рубрика %s, шаблон %s",
                 parsed["score"], parsed["rubric"], parsed.get("template", "-"))

    log.info(
        "модель: %d айтемів → %d готових постів (відмов: %d, зіпсованих відповідей: %d)",
        len(items), len(results), refused, broken,
    )
    return results

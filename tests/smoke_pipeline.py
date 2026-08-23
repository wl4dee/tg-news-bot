"""Наскрізний прогін пайплайну на ЖИВИХ джерелах із заглушеними Gemini і Telegram.

Навіщо: DRY_RUN перевіряє те саме, але потребує GEMINI_API_KEY і працює хвилини
через паузи під безкоштовний тариф. Цей тест проходить за секунди, не витрачає
квоту й ганяє справжні дані з реальних фідів — тобто ловить рівно ті поломки,
які видно тільки на живому матеріалі: зміни в розмітці t.me, мертві фіди,
порожні заголовки.

Запуск: python -m tests.smoke_pipeline
Мережа потрібна. Без неї тест чесно скаже, що джерела недоступні.
"""
from __future__ import annotations

import json
import logging
import sys

from bot import collect, dedupe, docs, publish
from bot.main import apply_limits, pick_candidates

logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
log = logging.getLogger("smoke")

FIXTURE = "config/sources.txt"

# Що модель нібито повернула. Формат — точно за контрактом із документа 01.
FAKE_POST = {
    "publish": True,
    "score": 8,
    "story_key": "smoke-test",
    "rubric": "крипта",
    "template": "Б",
    "text": '⚖️ SEC <a href="https://sec.gov/x">схвалила</a> запуск ETF.\n\n'
            "➤ обсяг $1T\n➤ старт 12.09\n\n<b>@channel</b>",
    "source_url": "https://sec.gov/x",
    "earlier_ids": [30912],
}


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    problems: list[str] = []

    # 1. Конфіг із локальної копії документа 02 (справжній доступ ще не відкрито).
    print("\n=== 1. парсер документа джерел ===")
    with open(FIXTURE, encoding="utf-8") as fh:
        config = docs.parse_sources(fh.read())
    if not config["sources"]:
        problems.append("парсер не дав жодного джерела")

    # 2. Живий збір. Беремо підмножину, щоб тест лишався швидким.
    print("\n=== 2. збір із живих джерел ===")
    state = {"seen": [], "published": [], "stats": [], "dead_sources": {}}
    # По одному джерелу з кожної рубрики, а не перші N підряд: інакше вибірка
    # виявляється однорідною (перші RSS у файлі — регулятори, які публікують
    # раз на тиждень) і передфільтр по рубриках лишається неперевіреним.
    subset = [s for s in config["sources"] if s["kind"] == "tg"][:1]
    for rubric in sorted({s["rubric"] for s in config["sources"]}):
        rss = [s for s in config["sources"]
               if s["kind"] == "rss" and s["rubric"] == rubric]
        if rss:
            subset.append(rss[-1])
    subset.append({"kind": "rss", "value": "https://dead.invalid/feed.xml",
                   "rubric": "техно", "weight": 1, "id": "dead"})

    items = collect.collect_all(subset, state)
    if not items:
        problems.append("з живих джерел не прийшло жодного айтема")
    if "https://dead.invalid/feed.xml" not in state["dead_sources"]:
        problems.append("мертве джерело не потрапило в dead_sources")

    empty_titles = [i for i in items if len(i.get("title", "").strip()) < 5]
    if empty_titles:
        problems.append(f"{len(empty_titles)} айтемів із порожнім заголовком")

    # 3. Дедуп на справжніх заголовках.
    print("\n=== 3. дедуп ===")
    fresh = dedupe.filter_new(items, state)
    hashes = [i["hash"] for i in fresh]
    if len(set(hashes)) != len(hashes):
        problems.append("після дедупу лишились однакові хеші")

    # Повторний прогін тих самих айтемів має відсіяти все.
    from bot.state import now_iso
    state["seen"] = [{"hash": str(h), "ts": now_iso()} for h in hashes]
    again = dedupe.filter_new(list(items), state)
    if again:
        problems.append(f"дедуп пропустив {len(again)} вже бачених айтемів")
    else:
        print("       повторний прогін відсіяв усе — seen[] працює")

    # 4. Передфільтр кандидатів.
    print("\n=== 4. передфільтр ===")
    candidates = pick_candidates(fresh, config["candidate_limit"], config["rubric_limits"])
    if len(candidates) > config["candidate_limit"]:
        problems.append("передфільтр перевищив ліміт кандидатів")
    rubrics = {c["rubric"] for c in candidates}
    print(f"       кандидатів {len(candidates)}, рубрик {len(rubrics)}: {sorted(rubrics)}")

    # 5. Ліміти. Модель заглушена — перевіряємо саме арифметику слотів.
    print("\n=== 5. ліміти рубрик і загальний ===")
    posts = []
    for index, item in enumerate(candidates):
        post = dict(FAKE_POST)
        post["rubric"] = item["rubric"]
        post["score"] = 10 - (index % 5)
        post["_item"] = item
        posts.append(post)

    chosen = apply_limits(posts, config["total_limit"], config["rubric_limits"])
    if len(chosen) > config["total_limit"]:
        problems.append(f"обрано {len(chosen)} постів при ліміті {config['total_limit']}")
    for rubric, cap in config["rubric_limits"].items():
        got = sum(1 for p in chosen if p["rubric"] == rubric)
        if got > cap:
            problems.append(f"рубрика {rubric}: {got} постів при ліміті {cap}")
    scores = [p["score"] for p in chosen]
    if scores != sorted(scores, reverse=True):
        problems.append("пости обрані не за спаданням score")

    # 6. Валідність HTML усіх обраних.
    print("\n=== 6. HTML під Telegram ===")
    for post in chosen:
        found = publish.validate_html(post["text"])
        if found:
            problems.append(f"невалідний HTML: {found[:2]}")
    print(f"       перевірено {len(chosen)} постів")

    # 7. Картка для KV: воркер має отримати все, що йому потрібно.
    print("\n=== 7. картка draft для воркера ===")
    if chosen:
        item = chosen[0]["_item"]
        card = {
            "story_key": chosen[0]["story_key"],
            "score": chosen[0]["score"],
            "rubric": chosen[0]["rubric"],
            "topic": chosen[0]["story_key"],
            "source_url": chosen[0]["source_url"],
            "ts": now_iso(),
            "drafts_message_id": 1,
            "raw_item": {k: item.get(k, "") for k in
                         ("title", "text", "url", "origin", "rubric", "weight",
                          "source_type", "source", "published_at")},
        }
        try:
            encoded = json.dumps(card, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            problems.append(f"картка не серіалізується в JSON: {exc}")
            encoded = ""
        # KV тримає до 25 МБ на значення, але роздувати картку сирим текстом ні до чого.
        if len(encoded.encode("utf-8")) > 60_000:
            problems.append("картка draft завелика")
        for field in ("story_key", "score", "rubric", "source_url"):
            if card.get(field) in (None, ""):
                problems.append(f"у картці немає {field} — воркер не залогує рішення")
        print(f"       картка {len(encoded.encode('utf-8'))} байт, "
              f"raw_item є: {bool(card['raw_item']['title'])}")

    # --- підсумок ---
    print("\n" + "=" * 60)
    if problems:
        print("ЗНАЙДЕНО ПРОБЛЕМИ:")
        for line in problems:
            print("  -", line)
        return 1
    print("OK: пайплайн проходить наскрізь на живих даних")
    print(f"    {len(items)} зібрано → {len(fresh)} нових → "
          f"{len(candidates)} кандидатів → {len(chosen)} чернеток")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

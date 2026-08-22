"""Дедуп через simhash по нормалізованому заголовку.

Навіщо simhash, а не хеш рядка: та сама новина приходить із п'яти фідів із трохи
різними заголовками («SEC схвалила ETF» / «SEC схвалила Ethereum ETF — деталі»).
Точний хеш їх не зловить, simhash — зловить.

Про поріг. Канонічні 3 біти з оригінальної статті розраховані на документи з
сотнями ознак. У заголовку з чотирьох слів ознак одиниці, і одне зайве слово
перевертає півхеша. Тому ознаки тут — слова ПЛЮС символьні 4-грами (їх завжди
десятки навіть у короткому рядку), а поріг відкалібрований емпірично на наборі
з `--selftest`: дублі лягають у 0-10 біт, різні новини — від 14. Поріг 12
стоїть у розриві. Міняєш ознаки — переганяй `--selftest` і став поріг заново.
"""
from __future__ import annotations

import logging
import re
import unicodedata
from datetime import timedelta

from bot.config import SEEN_TTL_HOURS
from bot.state import now, parse_iso

log = logging.getLogger(__name__)

HASH_BITS = 64
HAMMING_THRESHOLD = 12
SHINGLE = 4

_URL_RE = re.compile(r"https?://\S+|t\.me/\S+|www\.\S+")
_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize(title: str) -> str:
    """Заголовок → канонічна форма. Все, що не несе змісту, зникає."""
    text = unicodedata.normalize("NFKC", title or "").lower()
    text = _URL_RE.sub(" ", text)
    text = text.replace("ё", "е").replace("’", "").replace("'", "")
    text = _NON_WORD_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()


def features(title: str) -> list[str]:
    """Слова + символьні 4-грами. Слова тримають зміст, грами — стійкість
    до відмінків, скорочень і дрібних правок формулювання."""
    text = normalize(title)
    if not text:
        return []
    words = [w for w in text.split() if len(w) > 2]
    padded = text.replace(" ", "_")
    grams = [
        padded[i:i + SHINGLE]
        for i in range(max(1, len(padded) - SHINGLE + 1))
    ]
    return words + grams


def _fnv1a(token: str) -> int:
    """FNV-1a: детермінований між процесами, на відміну від вбудованого hash(),
    який рандомізується через PYTHONHASHSEED. Стабільність критична — seen[]
    переживає прогони, і хеші мусять збігатися між запусками."""
    h = 0xCBF29CE484222325
    for byte in token.encode("utf-8"):
        h ^= byte
        h = (h * 0x100000001B3) & 0xFFFFFFFFFFFFFFFF
    return h


def simhash(title: str) -> int:
    """64-бітний simhash. Чистий Python, без залежностей."""
    toks = features(title)
    if not toks:
        return 0

    vector = [0] * HASH_BITS
    for token in toks:
        h = _fnv1a(token)
        for bit in range(HASH_BITS):
            vector[bit] += 1 if (h >> bit) & 1 else -1

    result = 0
    for bit in range(HASH_BITS):
        if vector[bit] > 0:
            result |= 1 << bit
    return result


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def is_duplicate(candidate: int, known: list[int],
                 threshold: int = HAMMING_THRESHOLD) -> bool:
    return any(hamming(candidate, k) <= threshold for k in known)


def filter_new(items: list[dict], state: dict) -> list[dict]:
    """Прибрати те, що вже бачили за 48 год, і те, що дублюється всередині прогону.

    Кожному айтему проставляє поле `hash`. Стан не змінює — `seen` поповнюється
    в main.py тільки для тих айтемів, які реально дійшли до моделі.
    """
    cutoff = now() - timedelta(hours=SEEN_TTL_HOURS)
    known = [
        int(r["hash"])
        for r in state.get("seen", [])
        if (ts := parse_iso(r.get("ts", "")))
        and ts > cutoff
        and str(r.get("hash", "")).isdigit()
    ]

    fresh: list[dict] = []
    within_run: list[int] = []
    dup_seen = dup_run = 0

    for item in items:
        basis = item.get("title") or item.get("text", "")[:200]
        h = simhash(basis)
        item["hash"] = h

        if is_duplicate(h, known):
            dup_seen += 1
            continue
        if is_duplicate(h, within_run):
            dup_run += 1
            continue

        within_run.append(h)
        fresh.append(item)

    log.info(
        "дедуп: %d на вході → %d нових (відсіяно: %d як бачені за %d год, "
        "%d як дублі всередині прогону)",
        len(items), len(fresh), dup_seen, SEEN_TTL_HOURS, dup_run,
    )
    return fresh


# Калібрувальний набір. Не декорація: саме на ньому обраний поріг 12.
_CASES = [
    ("SEC схвалила Ethereum ETF", "SEC схвалила Ethereum ETF — деталі", True),
    ("SEC схвалила Ethereum ETF", "SEC схвалила ETF на Ethereum", True),
    ("Binance виплатить $4,3 млрд штрафу", "Binance виплатить 4.3 млрд штрафу", True),
    ("Нацбанк підвищив облікову ставку до 15,5%", "НБУ підвищив облікову ставку до 15,5%", True),
    ("Нацбанк підвищив облікову ставку до 15,5%", "Нацбанк знизив облікову ставку до 13%", False),
    ("SEC схвалила Ethereum ETF", "SEC approves Ethereum ETF", False),
    ("SEC схвалила Ethereum ETF", "ФРС залишила ставку без змін", False),
    ("У Києві відключення світла на 4 години", "У Львові відключення світла", False),
]


def _selftest() -> int:
    ok = True
    dup_max, diff_min = 0, HASH_BITS

    print(f"{'dist':>5} {'дубль':>6} {'треба':>6}  пара")
    for a, b, expected in _CASES:
        d = hamming(simhash(a), simhash(b))
        got = d <= HAMMING_THRESHOLD
        ok &= got == expected
        if expected:
            dup_max = max(dup_max, d)
        else:
            diff_min = min(diff_min, d)
        mark = "" if got == expected else "   <-- ПОМИЛКА"
        print(f"{d:>5} {str(got):>6} {str(expected):>6}  {a[:34]:34} | {b[:34]}{mark}")

    print(f"\nпоріг {HAMMING_THRESHOLD}: дублі до {dup_max}, різні від {diff_min}, "
          f"запас {diff_min - dup_max - 1} біт")
    if diff_min <= dup_max:
        print("класи перетинаються — поріг не рятує, треба міняти ознаки")
        ok = False

    # Детермінованість між процесами: без неї seen[] марний.
    expected_hash = 5583905265538899295
    actual = simhash("SEC схвалила Ethereum ETF")
    if actual != expected_hash:
        print(f"хеш нестабільний: {actual} != {expected_hash}")
        ok = False
    else:
        print("хеш стабільний між процесами: OK")

    print("\nПІДСУМОК:", "OK" if ok else "Є ПОМИЛКИ")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())
    print("використання: python -m bot.dedupe --selftest")

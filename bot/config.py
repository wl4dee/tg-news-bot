"""Єдине місце, де читаються секрети. Більше ніде os.environ бути не повинно."""
from __future__ import annotations

import os
import sys

# Опційно підхоплюємо .env для локальних прогонів. У CI його немає — і не треба.
def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

DRY_RUN = os.environ.get("DRY_RUN", "").strip() not in ("", "0", "false", "False")

# Дефолти, які МОЖНА тримати в коді: це не конфіг редакції, а параметри механіки.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_PAUSE_SEC = float(os.environ.get("GEMINI_PAUSE_SEC", "7"))
# 0 — мислення вимкнене (так треба для Flash: інакше воно з'їдає ліміт вихідних
# токенів і модель повертає порожню відповідь). -1 — не передавати поле взагалі,
# знадобиться для Pro-моделей, які нуль не приймають.
GEMINI_THINKING_BUDGET = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))
CF_KV_NAMESPACE = os.environ.get("CF_KV_NAMESPACE", "NEWSBOT")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

SEEN_TTL_HOURS = 48
PUBLISHED_TTL_DAYS = 14

# Резервні ліміти на випадок, якщо в документі 02 немає відповідних рядків limit.
FALLBACK_TOTAL_LIMIT = 6
FALLBACK_CANDIDATE_LIMIT = 12


class ConfigError(RuntimeError):
    """Немає того, без чого прогін безглуздий. Падаємо одразу й пояснюємо як полагодити."""


def env(name: str, required: bool = True, default: str = "") -> str:
    """Секрет з оточення. Значення НІКОЛИ не логується — навіть частково."""
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            raise ConfigError(
                f"Немає змінної оточення {name}.\n"
                f"У GitHub Actions: Settings → Secrets and variables → Actions → New secret.\n"
                f"Локально: створи файл .env поруч із CLAUDE.md і додай рядок {name}=..."
            )
        return default
    return value


def require_all(names: list[str]) -> None:
    """Перевірити всі секрети одразу, щоб не падати по одному через хвилину роботи."""
    missing = [n for n in names if not os.environ.get(n, "").strip()]
    if missing:
        raise ConfigError(
            "Не вистачає змінних оточення: " + ", ".join(missing) + ".\n"
            "У GitHub Actions вони живуть у Settings → Secrets and variables → Actions.\n"
            "Локально — у файлі .env (він у .gitignore, у репозиторій не потрапить)."
        )


def fail(message: str) -> None:
    """Людське падіння: без трейсбека, зрозумілою мовою, з інструкцією."""
    print(f"\n!!! {message}\n", file=sys.stderr)
    raise SystemExit(1)

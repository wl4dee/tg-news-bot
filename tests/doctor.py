"""Діагностика: перевірити кожну ланку окремо й сказати, що саме зламано.

Запуск:  python -m tests.doctor

Нічого нікуди не публікує. Усі виклики або read-only (`getMe`, `getChat`,
`getWebhookInfo`, список неймспейсів KV), або один-єдиний мінімальний запит
до моделі, щоб побачити, що вона реально відповідає.

Секрети НЕ друкуються — ні цілком, ні частинами. У виводі лише «є/немає»
і відповіді сервісів. Вивід можна показувати кому завгодно.
"""
from __future__ import annotations

import json
import os
import sys

import requests

OK, BAD, WARN = "  OK  ", " ЗЛАМАНО", " УВАГА "


def line(status: str, name: str, detail: str = "") -> None:
    print(f"[{status}] {name}" + (f"\n         {detail}" if detail else ""))


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    sys.path.insert(0, ".")
    from bot.config import GEMINI_MODEL, _load_dotenv
    _load_dotenv()

    broken: list[str] = []

    # --- 1. Змінні оточення ---------------------------------------------
    print("\n=== 1. Змінні оточення ===")
    required = ["TELEGRAM_BOT_TOKEN", "DRAFTS_CHAT_ID", "GEMINI_API_KEY"]
    optional = ["CF_ACCOUNT_ID", "CF_KV_TOKEN"]
    for name in required + optional:
        value = os.environ.get(name, "")
        if value:
            # Друкуємо ДОВЖИНУ, не значення. Довжина ловить зайвий пробіл
            # чи обрізаний токен, але сам секрет не розкриває.
            line(OK, f"{name} задано ({len(value)} символів)")
        elif name in required:
            line(BAD, f"{name} НЕ задано")
            broken.append(name)
        else:
            line(WARN, f"{name} не задано — прогін працюватиме без KV")

    if any(n in broken for n in required):
        print("\nБез обов'язкових змінних далі перевіряти нічого.")
        print("Локально вони беруться з файлу .env поруч із CLAUDE.md.")
        return 1

    # --- 2. Конфіг редакції ----------------------------------------------
    print("\n=== 2. Конфіг редакції ===")
    from bot import docs
    try:
        prompt = docs.load(docs.PROMPT_PATH, os.environ.get("PROMPT_DOC_URL", ""), "промпт")
        cfg = docs.parse_sources(
            docs.load(docs.SOURCES_PATH, os.environ.get("SOURCES_DOC_URL", ""), "джерела")
        )
        line(OK, f"промпт {len(prompt)} символів, джерел {len(cfg['sources'])}")
        line(OK, f"ліміти: всього {cfg['total_limit']}, кандидатів {cfg['candidate_limit']}, "
                 f"рубрики {cfg['rubric_limits']}")
    except Exception as exc:
        line(BAD, "конфіг не читається", str(exc)[:300])
        return 1

    # --- 3. Telegram ------------------------------------------------------
    print("\n=== 3. Telegram ===")
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    api = f"https://api.telegram.org/bot{token}"

    def tg(method: str, params: dict | None = None) -> dict:
        try:
            return requests.get(f"{api}/{method}", params=params or {}, timeout=20).json()
        except Exception as exc:
            return {"ok": False, "description": f"{type(exc).__name__}: {exc}"}

    me = tg("getMe")
    if me.get("ok"):
        line(OK, f"токен робочий, бот @{me['result'].get('username')}")
    else:
        line(BAD, "TELEGRAM_BOT_TOKEN не працює", str(me.get("description"))[:200])
        broken.append("токен Telegram")

    chat = tg("getChat", {"chat_id": os.environ["DRAFTS_CHAT_ID"]})
    if chat.get("ok"):
        r = chat["result"]
        line(OK, f"група чернеток: «{r.get('title')}», тип {r.get('type')}")
        if r.get("type") == "group":
            line(WARN, "це звичайна група, не супергрупа",
                 "якщо вона колись стане супергрупою, її id зміниться "
                 "і чернетки перестануть доходити")
    else:
        line(BAD, "DRAFTS_CHAT_ID не працює", str(chat.get("description"))[:200])
        broken.append("DRAFTS_CHAT_ID")

    hook = tg("getWebhookInfo")
    if hook.get("ok"):
        info = hook["result"]
        url = info.get("url", "")
        if url:
            line(OK, f"вебхук стоїть: {url}")
            if info.get("pending_update_count"):
                line(WARN, f"необроблених апдейтів: {info['pending_update_count']}")
            if info.get("last_error_message"):
                line(BAD, "вебхук відповідає з помилкою",
                     f"{info.get('last_error_message')} (востаннє: {info.get('last_error_date')})")
                broken.append("вебхук віддає помилку")
        else:
            line(WARN, "вебхук НЕ встановлений",
                 "кнопки під чернетками працювати не будуть — це крок E")

    # --- 4. Gemini --------------------------------------------------------
    print(f"\n=== 4. Gemini ({GEMINI_MODEL}) ===")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "contents": [{"role": "user", "parts": [{"text":
            'Поверни рівно такий JSON і нічого більше: {"publish": false, "reason": "перевірка"}'}]}],
        "generationConfig": {
            "temperature": 0,
            "maxOutputTokens": 4096,
            "response_mime_type": "application/json",
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        resp = requests.post(url, params={"key": os.environ["GEMINI_API_KEY"]},
                             json=payload, timeout=60)
        data = resp.json()
    except Exception as exc:
        line(BAD, "Gemini недоступний", f"{type(exc).__name__}: {exc}")
        data, resp = {}, None
        broken.append("Gemini")

    if resp is not None:
        if resp.status_code != 200:
            msg = str((data.get("error") or {}).get("message", ""))[:250]
            line(BAD, f"Gemini HTTP {resp.status_code}", msg)
            if "API key not valid" in msg:
                line(BAD, "→ GEMINI_API_KEY невірний", "візьми новий на aistudio.google.com")
            elif "not found" in msg.lower() or resp.status_code == 404:
                line(BAD, f"→ модель {GEMINI_MODEL} недоступна",
                     "задай іншу через змінну GEMINI_MODEL")
            broken.append("Gemini")
        else:
            cand = (data.get("candidates") or [{}])[0]
            finish = cand.get("finishReason", "?")
            try:
                text = cand["content"]["parts"][0]["text"]
            except (KeyError, IndexError):
                text = ""
            usage = data.get("usageMetadata", {})
            if text:
                line(OK, f"модель відповіла, finishReason={finish}")
                line(OK, f"відповідь: {text.strip()[:120]}")
                line(OK, f"токени: {json.dumps(usage, ensure_ascii=False)}")
            else:
                line(BAD, f"модель повернула ПОРОЖНЮ відповідь, finishReason={finish}",
                     f"usage={json.dumps(usage, ensure_ascii=False)}")
                if finish == "MAX_TOKENS":
                    line(BAD, "→ ліміт вихідних токенів вичерпано на мисленні",
                         "підніми MAX_OUTPUT_TOKENS у bot/generate.py")
                broken.append("Gemini повертає порожнє")

    # --- 5. Cloudflare KV -------------------------------------------------
    print("\n=== 5. Cloudflare KV ===")
    if not (os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_KV_TOKEN")):
        line(WARN, "пропущено — немає CF_ACCOUNT_ID/CF_KV_TOKEN")
    else:
        try:
            from bot.kv import KV
            kv = KV()
            ns = kv.resolve_namespace()
            line(OK, "неймспейс NEWSBOT знайдено")
            for prefix in ("draft:", "log:", "retry:"):
                keys = kv.list_keys(prefix)
                line(OK, f"ключів {prefix:8} {len(keys)}"
                         + (f"  напр. {keys[0]}" if keys else ""))
        except Exception as exc:
            line(BAD, "KV недоступний", str(exc)[:250])
            broken.append("Cloudflare KV")

    # --- підсумок ---------------------------------------------------------
    print("\n" + "=" * 62)
    if broken:
        print("ЗЛАМАНО:")
        for item in dict.fromkeys(broken):
            print("  -", item)
        return 1
    print("Усі ланки живі.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

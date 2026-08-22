"""Telegram Bot API. ТІЛЬКИ чернетки.

У цьому модулі свідомо немає CHANNEL_ID, copyMessage і getUpdates.
Публікація в канал існує рівно в одному місці проєкту — worker/src/index.js,
і тільки у відповідь на натискання кнопки людиною. Якщо колись захочеться
додати сюди відправку в канал — це і є та помилка, від якої проєкт відгороджується.

Апдейти Telegram тут не читаються ніде: вебхук назавжди вимикає getUpdates.
"""
from __future__ import annotations

import logging
import re
import time
import uuid

import requests

from bot.config import DRY_RUN, env

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

# Пауза між чернетками: ліміт групи ~20 повідомлень/хв.
DRAFT_PAUSE_SEC = 3.0

# Рівно ті теги, які розуміє Telegram. Усе інше — 400 can't parse entities.
ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "blockquote", "span", "tg-spoiler",
}
# Теги, які модель любить вигадувати, бо звикла до вебу.
FORBIDDEN_TAGS = {"br", "p", "div", "ul", "ol", "li", "h1", "h2", "h3", "hr", "img"}

_TAG_RE = re.compile(r"<\s*(/?)\s*([a-zA-Z][a-zA-Z0-9-]*)([^>]*)>")


def new_draft_id() -> str:
    """Короткий id: у callback_data всього 64 байти, а «p:» вже з'їдає два."""
    return uuid.uuid4().hex[:8]


def validate_html(text: str) -> list[str]:
    """Перевірити розмітку під Telegram. Повертає список проблем, порожній = все добре.

    Це не повноцінний парсер HTML, а саме перевірка під звужений набір тегів
    Telegram. Дивись .claude/rules/telegram-api.md, розділ «HTML-розмітка».
    """
    problems: list[str] = []
    stack: list[str] = []

    for match in _TAG_RE.finditer(text):
        closing, tag, _attrs = match.group(1), match.group(2).lower(), match.group(3)

        if tag in FORBIDDEN_TAGS:
            problems.append(f"тег <{tag}> не існує в Telegram")
            continue
        if tag not in ALLOWED_TAGS:
            problems.append(f"невідомий тег <{tag}>")
            continue

        if closing:
            if not stack:
                problems.append(f"</{tag}> без відкриття")
            elif stack[-1] != tag:
                problems.append(f"</{tag}> закриває <{stack[-1]}>")
                stack.pop()
            else:
                stack.pop()
        else:
            stack.append(tag)

    for tag in stack:
        problems.append(f"<{tag}> не закритий")

    # Голий & ламає парсер так само надійно, як кривий тег.
    for bad in re.finditer(r"&(?!amp;|lt;|gt;|quot;|#\d+;|[a-z]+;)", text):
        problems.append(f"неекранований & на позиції {bad.start()}")
        break

    if len(text) > 4096:
        problems.append(f"довжина {len(text)} > 4096")

    return problems


class Telegram:
    def __init__(self) -> None:
        self._token = env("TELEGRAM_BOT_TOKEN")
        self.drafts_chat_id = env("DRAFTS_CHAT_ID")
        self.session = requests.Session()

    def _call(self, method: str, payload: dict, attempts: int = 3) -> dict | None:
        """Виклик API з повагою до retry_after. Помилка → None, ніколи не виняток."""
        url = API.format(token=self._token, method=method)

        for attempt in range(1, attempts + 1):
            try:
                resp = self.session.post(url, json=payload, timeout=30)
            except requests.RequestException as exc:
                log.warning("Telegram %s: мережа впала (%d/%d): %s",
                            method, attempt, attempts, exc)
                time.sleep(2 ** attempt)
                continue

            try:
                data = resp.json()
            except ValueError:
                log.error("Telegram %s: відповідь не JSON (HTTP %s)",
                          method, resp.status_code)
                return None

            if data.get("ok"):
                return data.get("result")

            description = str(data.get("description", ""))[:200]

            if resp.status_code == 429:
                # Telegram сам каже, скільки чекати. Свій бекоф тут гірший.
                wait = int((data.get("parameters") or {}).get("retry_after", 5))
                log.warning("Telegram %s: 429, чекаємо %d с", method, wait)
                time.sleep(wait + 1)
                continue

            # Звичайна група стала супергрупою — її номер змінився.
            # Без цієї гілки прогін мовчав би «chat not found», і шукати
            # причину довелось би вручну. Telegram віддає новий id тут же.
            new_id = (data.get("parameters") or {}).get("migrate_to_chat_id")
            if new_id:
                log.error(
                    "Група «Чернетки» перетворилась на супергрупу, її номер змінився.\n"
                    "    Новий DRAFTS_CHAT_ID: %s\n"
                    "    Онови секрет у GitHub: Settings → Secrets and variables → Actions.",
                    new_id,
                )
                return None

            # Токен у логи не потрапляє: у description його немає.
            log.error("Telegram %s: %s", method, description)
            return None

        return None

    def verify(self) -> bool:
        """Перевірити токен і доступ до групи ДО генерації.

        Обидва виклики read-only — нічого нікуди не надсилається, навіть у
        бойовому режимі. Сенс у тому, щоб зламаний токен чи неправильний
        DRAFTS_CHAT_ID виявились за дві секунди, а не після півтори хвилини
        роботи моделі й витраченої квоти безкоштовного тарифу.
        """
        me = self._call("getMe", {}, attempts=1)
        if not me:
            log.error(
                "TELEGRAM_BOT_TOKEN не працює.\n"
                "    Перевір секрет у GitHub: Settings → Secrets and variables → Actions.\n"
                "    Якщо токен загублено — @BotFather → /mybots → API Token."
            )
            return False
        log.info("бот @%s на зв'язку", me.get("username", "?"))

        chat = self._call("getChat", {"chat_id": self.drafts_chat_id}, attempts=1)
        if not chat:
            log.error(
                "Група чернеток недоступна за DRAFTS_CHAT_ID.\n"
                "    Найчастіші причини: бота не додано в групу, або номер групи\n"
                "    змінився після підвищення до супергрупи, або в секреті зайвий пробіл."
            )
            return False
        log.info(
            "група чернеток: «%s» (%s)",
            chat.get("title", "?"), chat.get("type", "?"),
        )
        return True

    def send_draft(self, draft_id: str, text: str, meta: str) -> int | None:
        """Чернетка в групу з трьома кнопками. Повертає message_id або None."""
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Опублікувати", "callback_data": f"p:{draft_id}"},
                {"text": "✏️ Переписати", "callback_data": f"r:{draft_id}"},
                {"text": "❌ Відхилити", "callback_data": f"x:{draft_id}"},
            ]]
        }

        if DRY_RUN:
            print("\n" + "─" * 72)
            print(f"  [DRY_RUN] чернетка {draft_id} → чат {self.drafts_chat_id}")
            print(f"  {meta}")
            print("─" * 72)
            print(text)
            print("─" * 72)
            print("  кнопки: ✅ p:%s | ✏️ r:%s | ❌ x:%s" % (draft_id, draft_id, draft_id))
            return -1

        result = self._call("sendMessage", {
            "chat_id": self.drafts_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": keyboard,
            # Прев'ю джерела чіпляє під пост чужу картку з фото й заголовком
            # видання — виглядає як реклама першоджерела, а не як наш пост.
            # Лінк у ліді нікуди не дівається, просто без картки.
            "disable_web_page_preview": True,
        })
        if not result:
            return None
        return result.get("message_id")

    def notify(self, text: str) -> None:
        """Службове повідомлення в групу чернеток. Не критично, якщо не дійде."""
        if DRY_RUN:
            print(f"  [DRY_RUN] службове повідомлення: {text}")
            return
        self._call("sendMessage", {
            "chat_id": self.drafts_chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        })

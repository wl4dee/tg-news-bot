# tg-news-bot

Новинний Telegram-канал, який збирає матеріал сам, переписує його у фірмовому
стилі й публікує **тільки після натискання кнопки ✅**.

Автопублікації немає за проєктом: жоден шлях у коді не пише в канал без
callback від кнопки.

```
Google Docs ──┐
              │  промпт + джерела, читаються щопрогону
              ▼
cron */30 → ЗБИРАЧ (Python, GitHub Actions)
              │  RSS + t.me/s/ → дедуп → Gemini → чернетка в групу
              ▼
         група «Чернетки»  [✅ Опублікувати] [✏️ Переписати] [❌ Відхилити]
              │
              │ натискання
              ▼
         ВОРКЕР (Cloudflare) → copyMessage у канал → log у KV
              │
              ▼
         збирач наступного прогону забирає log → state.json
```

Два компоненти, бо вебхук і `getUpdates` взаємовиключні: після `setWebhook`
метод `getUpdates` мертвий назавжди. Тому збирач ніколи не читає апдейти
Telegram — усі вони йдуть у воркер.

## Що де лежить

| | |
|---|---|
| `bot/` | збирач: `main` (оркестратор), `docs`, `collect`, `dedupe`, `generate`, `publish`, `state`, `kv`, `config` |
| `worker/` | вебхук Cloudflare — єдине місце, що пише в канал |
| `tests/` | інваріанти, наскрізний smoke-прогін, фікстура документа 02 |
| `.claude/` | правила, слеш-команди, агент-критик стилю, хук на секрети |
| `state.json` | історія: `seen`, `published`, `stats`, `dead_sources` |

Промпт і джерела в коді **не лежать**. Вони в Google Docs і правляться з телефону.

## Запуск

### 1. Telegram

1. Створи канал (приватний) і групу «Чернетки».
2. `@BotFather` → `/newbot` → `TELEGRAM_BOT_TOKEN`.
3. Додай бота адміном у канал (право «Публікація повідомлень») і в групу
   (адмін потрібен, щоб він міг редагувати кнопки під постом).
4. Дізнайся `chat_id` обох чатів **зараз, поки вебхука немає**: напиши щось
   у групу, опублікуй щось у канал, відкрий у браузері
   `https://api.telegram.org/bot<ТОКЕН>/getUpdates` і знайди `chat.id`
   (обидва починаються з `-100`).

Після кроку 8 цей метод перестане працювати назавжди. Це нормально.

### 2. Документи

Обидва документи: **Поділитися → Доступ за посиланням → Читач**.
Без цього збирач не стартує — `/export?format=txt` віддасть 401.

`PROMPT_DOC_URL` і `SOURCES_DOC_URL` — це посилання виду
`https://docs.google.com/document/d/<ID>/export?format=txt`.

### 3. Cloudflare

```bash
cd worker && npm install
npx wrangler kv namespace create NEWSBOT
```

Отриманий `id` встав у `worker/wrangler.toml`, туди ж — свій `CHANNEL_ID`.
Назву `NEWSBOT` не міняй: збирач резолвить неймспейс саме за нею.

```bash
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put WEBHOOK_SECRET
npx wrangler deploy
```

В API-токені Cloudflare потрібне право **Workers KV Storage: Edit** —
з нього беруться `CF_ACCOUNT_ID` і `CF_KV_TOKEN`.

### 4. Секрети GitHub

Settings → Secrets and variables → Actions:

```
TELEGRAM_BOT_TOKEN  DRAFTS_CHAT_ID  GEMINI_API_KEY
PROMPT_DOC_URL      SOURCES_DOC_URL
CF_ACCOUNT_ID       CF_KV_TOKEN
```

`CHANNEL_ID` серед них немає навмисно: збирачу він не потрібен і не має бути
доступний. Канал знає тільки воркер.

### 5. Вебхук — останнім

```
https://api.telegram.org/bot<ТОКЕН>/setWebhook?url=<URL_ВОРКЕРА>&secret_token=<WEBHOOK_SECRET>
```

Після цього `getUpdates` мертвий. Саме тому крок 1.4 робиться до нього.

### 6. Перший прогін

Actions → `collect` → Run workflow. У групі мають з'явитись чернетки з кнопками.
Тиснеш ✅ — пост у каналі за секунду.

## Локально

```bash
pip install -r requirements.txt
```

Створи `.env` (він у `.gitignore`) із тими самими змінними й запусти сухий прогін:

```bash
DRY_RUN=1 python -m bot.main
```

`DRY_RUN=1` — усе працює, але нічого нікуди не відправляється: чернетки
друкуються в консоль, у KV нічого не пишеться, `state.json` не комітиться.
Без `CF_ACCOUNT_ID`/`CF_KV_TOKEN` сухий прогін теж пройде — просто без KV.

## Тести

```bash
python -m tests.check_invariants
```

```bash
python -m bot.dedupe --selftest
```

```bash
python -m tests.smoke_pipeline
```

```bash
node worker/test/handler.test.mjs
```

- `check_invariants` — доводить по AST, що в `bot/` немає ні доступу до каналу,
  ні `getUpdates`, ні видалення повідомлень.
- `dedupe --selftest` — калібрувальний набір, на якому обраний поріг simhash.
- `smoke_pipeline` — наскрізний прогін на живих джерелах із заглушеними
  Gemini і Telegram. Ловить зміни розмітки `t.me` і мертві фіди.
- `handler.test.mjs` — воркер із мок-KV: чужий секрет, подвійне ✅, збій
  `copyMessage`, кнопка ✏️.

## Слеш-команди

| | |
|---|---|
| `/test-run` | сухий прогін із перевіркою інваріантів і розбором виводу |
| `/add-source` | перевірити фід на 200 + валідний XML і видати рядок для документа 02 |
| `/stats` | конверсія published/rejected по бакетах score |

Агент `style-critic` звіряє згенеровані пости з документом 01 і каже, що правити —
пост чи саме формулювання в документі.

## Правило проєкту

Стиль і джерела правляться в Google Docs. Код чіпається тільки коли ламається
механіка. Повний перелік того, чого робити не можна — у `CLAUDE.md`.

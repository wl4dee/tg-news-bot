# tg-news-bot

Новинний Telegram-канал, який збирає матеріал сам, переписує його у фірмовому
стилі й публікує **тільки після натискання кнопки ✅**.

Автопублікації немає за проєктом: жоден шлях у коді не пише в канал без
callback від кнопки.

```
config/ ──────┐
              │  prompt.md + sources.txt, читаються щопрогону
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
| `config/` | **промпт стилю і джерела** — те, що правиться найчастіше |
| `tests/` | інваріанти й наскрізний smoke-прогін на живих джерелах |
| `.claude/` | правила, слеш-команди, агент-критик стилю, хук на секрети |
| `state.json` | історія: `seen`, `published`, `stats`, `dead_sources` |

Промпт і джерела в коді **не лежать**. Вони в `config/`: правка стилю — це коміт,
а не деплой. Редагувати можна через Claude Code, веб-редактор GitHub із телефону
або звичайним комітом.

## Запуск

### 1. Telegram

1. Створи канал (**публічний** — інакше лінки «Раніше:» не працюватимуть
   для нових читачів) і приватну групу «Чернетки».
2. `@BotFather` → `/newbot` → `TELEGRAM_BOT_TOKEN`.
3. Додай бота адміном у канал (право «Публікація повідомлень») і в групу
   (адмін потрібен, щоб він міг редагувати кнопки під постом).
4. Дізнайся id групи чернеток **зараз, поки вебхука немає**: напиши в групу
   `/start@ім'я_бота`, тоді відкрий `https://api.telegram.org/bot<ТОКЕН>/getUpdates`
   і візьми `chat.id` із блоку, де `"type":"group"` або `"supergroup"`.
   Це `DRAFTS_CHAT_ID`.

Id каналу не потрібен: для публічного каналу воркер приймає `@username`.

Після кроку 5 `getUpdates` перестане працювати назавжди. Це нормально.

### 2. Конфіг редакції

Нічого налаштовувати не треба — `config/prompt.md` і `config/sources.txt` уже
в репозиторії. Стиль правиться в першому, джерела в другому.

Якщо колись захочеться тримати їх у Google Docs — задай `PROMPT_DOC_URL` і
`SOURCES_DOC_URL` (формат `.../export?format=txt`, документ має бути відкритий
за посиланням). Тоді файли ігноруються.

### 3. Cloudflare

```bash
cd worker && npm install
npx wrangler kv namespace create NEWSBOT
```

Отриманий `id` встав у `worker/wrangler.toml`, туди ж — `CHANNEL_ID` у вигляді
`@username` каналу. Назву `NEWSBOT` не міняй: збирач резолвить неймспейс за нею.

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

Створи `.env` (він у `.gitignore`) із тими самими п'ятьма змінними й запусти сухий прогін:

```bash
DRY_RUN=1 python -m bot.main
```

`DRY_RUN=1` — усе працює, але нічого нікуди не відправляється: чернетки
друкуються в консоль, у KV нічого не пишеться, `state.json` не комітиться.
Без `CF_ACCOUNT_ID`/`CF_KV_TOKEN` сухий прогін теж пройде — просто без KV.

Перед генерацією прогін робить `getMe` і `getChat` — обидва read-only. Зламаний
токен або неправильний `DRAFTS_CHAT_ID` виявляються за дві секунди, а не після
витраченої квоти моделі.

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
| `/add-source` | перевірити фід на 200 + валідний XML і дописати його в `config/sources.txt` |
| `/stats` | конверсія published/rejected по бакетах score |

Агент `style-critic` звіряє згенеровані пости з `config/prompt.md` і каже, що
правити — пост чи саме формулювання в промпті.

## Правило проєкту

Стиль і джерела правляться в `config/`. Код чіпається тільки коли ламається
механіка. Повний перелік того, чого робити не можна — у `CLAUDE.md`.

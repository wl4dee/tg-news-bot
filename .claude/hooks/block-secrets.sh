#!/usr/bin/env bash
# PreToolUse hook: не дати токену потрапити у файл, у коміт або в лог.
# Читає JSON-подію зі stdin. exit 2 = заблокувати виклик і показати причину моделі.
set -uo pipefail

payload="$(cat)"

# Витягуємо все, що модель збирається записати або виконати.
# Без jq — він є не всюди; беремо весь payload, помилки тут дорожчі за зайвий скан.
haystack="$payload"

# 1) Токен Telegram: 8-10 цифр, двокрапка, 35 символів base64url.
tg='[0-9]{8,10}:[A-Za-z0-9_-]{35}'
# 2) Ключ Google/Gemini.
goog='AIza[0-9A-Za-z_-]{35}'
# 3) Токен Cloudflare API: 40 символів base64url після типової назви поля.
cf='(CF_KV_TOKEN|CF_API_TOKEN|cloudflare[_-]?token)["'"'"':= ]+[A-Za-z0-9_-]{40}'
# 4) Явно вписаний секрет вебхука.
wh='WEBHOOK_SECRET["'"'"':= ]+[A-Za-z0-9_-]{16,}'

for pat in "$tg" "$goog" "$cf" "$wh"; do
  if printf '%s' "$haystack" | grep -Eq "$pat"; then
    echo "BLOCKED: у вмісті виклику знайдено щось схоже на справжній секрет." >&2
    echo "Секрети беруться тільки з env (os.environ / env.SECRET), у файли не пишуться." >&2
    echo "Якщо це плейсхолдер — зроби його очевидним: <TELEGRAM_BOT_TOKEN>, xxx, 0000." >&2
    exit 2
  fi
done

# 5) Окремо: не даємо закомітити .env / .dev.vars, навіть якщо всередині чисто.
if printf '%s' "$haystack" | grep -Eq 'git +(add|commit).*(\.env|\.dev\.vars)'; then
  echo "BLOCKED: .env і .dev.vars не комітяться. Вони в .gitignore." >&2
  exit 2
fi

exit 0

/**
 * Вебхук Telegram. Обробляє натискання кнопок під чернетками.
 *
 * Це ЄДИНЕ місце в проєкті, яке має право писати в канал, і робить це лише
 * у відповідь на callback_query від кнопки ✅. Збирач у канал не пише ніколи.
 *
 * Секрети (wrangler secret put): TELEGRAM_BOT_TOKEN, WEBHOOK_SECRET
 * Змінні (wrangler.toml vars):   CHANNEL_ID
 * KV binding:                    NEWSBOT
 */

const BUTTONS = {
  p: { label: "✅ Опубліковано", decision: "pub", toast: "Опубліковано в канал" },
  x: { label: "❌ Відхилено", decision: "rej", toast: "Відхилено" },
  r: { label: "✏️ У черзі на перегенерацію", decision: null, toast: "Перегенерую наступним прогоном" },
};

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }

    // Без цієї перевірки ендпоінт публічний і будь-хто публікує в канал.
    // Порівняння постійного часу — щоб секрет не можна було підібрати за таймінгом.
    const provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token") || "";
    if (!safeEqual(provided, env.WEBHOOK_SECRET || "")) {
      return new Response("forbidden", { status: 403 });
    }

    let update;
    try {
      update = await request.json();
    } catch {
      return ok();
    }

    // Обробляємо тільки натискання кнопок. Решта апдейтів — тиша і 200,
    // інакше Telegram влаштує ретрай-шторм.
    const query = update.callback_query;
    if (!query) return ok();

    try {
      await handleCallback(query, env);
    } catch (err) {
      // Помилка не має перетворитись на 500: Telegram ретраїтиме, і при ✅
      // це означає другий пост у каналі. Логуємо і віддаємо 200.
      console.error("callback failed:", err && err.stack ? err.stack : String(err));
      await answer(env, query.id, "Помилка обробки, спробуй ще раз").catch(() => {});
    }

    return ok();
  },
};

async function handleCallback(query, env) {
  const data = query.data || "";
  const [action, draftId] = splitOnce(data, ":");
  const spec = BUTTONS[action];

  // Кнопка вже оброблена й перетворена на напис — на неї теж можна натиснути.
  if (!spec || !draftId) {
    await answer(env, query.id, "");
    return;
  }

  const draftsChatId = query.message && query.message.chat && query.message.chat.id;
  const draftMessageId = query.message && query.message.message_id;
  if (!draftsChatId || !draftMessageId) {
    await answer(env, query.id, "Не бачу повідомлення чернетки");
    return;
  }

  // Ідемпотентність. Telegram ретраїть вебхуки при таймауті, а подвійне ✅
  // без цієї перевірки дало б два однакові пости в каналі.
  const already = await env.NEWSBOT.get(`log:${draftId}`);
  if (already) {
    await answer(env, query.id, "Це вже оброблено");
    return;
  }

  const card = (await env.NEWSBOT.get(`draft:${draftId}`, { type: "json" })) || {};

  if (action === "r") {
    await env.NEWSBOT.put(
      `retry:${draftId}`,
      JSON.stringify({ ts: nowIso(), rubric: card.rubric || "" })
    );
    await setLabel(env, draftsChatId, draftMessageId, spec.label);
    await answer(env, query.id, spec.toast);
    return;
  }

  let channelMessageId = null;

  if (action === "p") {
    // reply_markup свідомо не передаємо: у канал пост іде без кнопок.
    // from_chat_id беремо з самого апдейта — це і є чат чернеток,
    // тому воркеру не потрібен DRAFTS_CHAT_ID як окремий секрет.
    const copied = await tg(env, "copyMessage", {
      chat_id: env.CHANNEL_ID,
      from_chat_id: draftsChatId,
      message_id: draftMessageId,
    });

    if (!copied || !copied.ok) {
      const why = copied && copied.description ? copied.description : "невідома помилка";
      await answer(env, query.id, `Не вийшло опублікувати: ${why}`.slice(0, 200));
      return; // лог не пишемо: рішення не відбулося, кнопка лишається робочою
    }
    channelMessageId = copied.result.message_id;
  }

  // Спершу лог у KV, потім косметика. Якщо впаде editMessageReplyMarkup —
  // рішення все одно збережене, і збирач його побачить.
  await env.NEWSBOT.put(
    `log:${draftId}`,
    JSON.stringify({
      story_key: card.story_key || "",
      score: card.score || 0,
      rubric: card.rubric || "",
      topic: card.topic || card.story_key || "",
      source_url: card.source_url || "",
      decision: spec.decision,
      channel_message_id: channelMessageId,
      ts: nowIso(),
    })
  );

  await setLabel(env, draftsChatId, draftMessageId, spec.label);
  await answer(env, query.id, spec.toast);
}

/** Замінити кнопки на один напис. Чернетку НЕ видаляємо — вона потрібна для статистики. */
async function setLabel(env, chatId, messageId, label) {
  await tg(env, "editMessageReplyMarkup", {
    chat_id: chatId,
    message_id: messageId,
    reply_markup: { inline_keyboard: [[{ text: label, callback_data: "noop" }]] },
  });
}

/** answerCallbackQuery обов'язковий: без нього кнопка висить «годинником» ~15 с. */
async function answer(env, callbackQueryId, text) {
  return tg(env, "answerCallbackQuery", {
    callback_query_id: callbackQueryId,
    text: text ? text.slice(0, 200) : "",
  });
}

async function tg(env, method, payload) {
  // Без цієї перевірки відсутній секрет дає найгіршу з можливих поломок: усі
  // виклики летять на /botundefined/, Telegram віддає 404, воркер повертає 200,
  // а кнопка просто крутиться вічно. Мовчазний збій, який нічим себе не видає.
  if (!env.TELEGRAM_BOT_TOKEN) {
    console.error(
      "TELEGRAM_BOT_TOKEN не заданий у секретах воркера. " +
        "Полагодити: npx wrangler secret put TELEGRAM_BOT_TOKEN"
    );
    return { ok: false, description: "worker has no TELEGRAM_BOT_TOKEN" };
  }

  const resp = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  try {
    return await resp.json();
  } catch {
    return { ok: false, description: `HTTP ${resp.status}` };
  }
}

function splitOnce(value, sep) {
  const at = value.indexOf(sep);
  return at === -1 ? [value, ""] : [value.slice(0, at), value.slice(at + 1)];
}

function safeEqual(a, b) {
  if (a.length !== b.length || a.length === 0) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function nowIso() {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

function ok() {
  return new Response("ok", { status: 200 });
}

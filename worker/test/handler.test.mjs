/**
 * Тести воркера без мережі й без Cloudflare: мок-KV і перехоплений fetch.
 * Запуск: node worker/test/handler.test.mjs
 *
 * Перевіряється головне, заради чого воркер існує: у канал не можна потрапити
 * без кнопки, без правильного секрету і двічі поспіль.
 */
import worker from "../src/index.js";

const SECRET = "test-webhook-secret-value";
const CHANNEL = "-1001111111111";
const DRAFTS = -1002222222222;

let calls = [];

function mockKV(initial = {}) {
  const store = new Map(Object.entries(initial));
  return {
    store,
    async get(key, opts) {
      const raw = store.get(key);
      if (raw === undefined) return null;
      return opts && opts.type === "json" ? JSON.parse(raw) : raw;
    },
    async put(key, value) {
      store.set(key, value);
    },
  };
}

function makeEnv(kvInit = {}) {
  return {
    TELEGRAM_BOT_TOKEN: "<token>",
    WEBHOOK_SECRET: SECRET,
    CHANNEL_ID: CHANNEL,
    NEWSBOT: mockKV(kvInit),
  };
}

globalThis.fetch = async (url, init) => {
  const method = String(url).split("/").pop();
  const body = JSON.parse(init.body);
  calls.push({ method, body });

  if (method === "copyMessage") {
    if (body.__fail) return jsonResp({ ok: false, description: "forced" });
    return jsonResp({ ok: true, result: { message_id: 555 } });
  }
  return jsonResp({ ok: true, result: true });
};

function jsonResp(obj) {
  return { status: 200, json: async () => obj };
}

function callbackUpdate(data) {
  return {
    callback_query: {
      id: "cbq-1",
      data,
      message: { message_id: 42, chat: { id: DRAFTS } },
    },
  };
}

function request(update, secret = SECRET, method = "POST") {
  return new Request("https://worker.example/", {
    method,
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Bot-Api-Secret-Token": secret,
    },
    body: method === "POST" ? JSON.stringify(update) : undefined,
  });
}

const DRAFT_CARD = JSON.stringify({
  story_key: "sec-etf",
  score: 8,
  rubric: "крипта",
  topic: "sec-etf",
  source_url: "https://sec.gov/x",
});

// ---------------------------------------------------------------- тести

let passed = 0;
let failed = 0;

async function test(name, fn) {
  calls = [];
  try {
    await fn();
    console.log(`  OK   ${name}`);
    passed++;
  } catch (err) {
    console.log(`  FAIL ${name}\n         ${err.message}`);
    failed++;
  }
}

function assert(cond, msg) {
  if (!cond) throw new Error(msg);
}

function used(method) {
  return calls.filter((c) => c.method === method);
}

console.log("воркер:");

await test("GET відхиляється (405)", async () => {
  const resp = await worker.fetch(request(null, SECRET, "GET"), makeEnv());
  assert(resp.status === 405, `очікували 405, отримали ${resp.status}`);
});

await test("невірний секрет → 403 і жодного виклику API", async () => {
  const resp = await worker.fetch(request(callbackUpdate("p:abc"), "wrong"), makeEnv());
  assert(resp.status === 403, `очікували 403, отримали ${resp.status}`);
  assert(calls.length === 0, "воркер поліз в API при невірному секреті");
});

await test("порожній секрет не проходить", async () => {
  const resp = await worker.fetch(request(callbackUpdate("p:abc"), ""), makeEnv());
  assert(resp.status === 403, `очікували 403, отримали ${resp.status}`);
});

await test("не-callback апдейт → 200 і тиша", async () => {
  const env = makeEnv();
  const resp = await worker.fetch(request({ message: { text: "привіт" } }), env);
  assert(resp.status === 200, "має бути 200");
  assert(calls.length === 0, "на звичайне повідомлення воркер не має реагувати");
});

await test("✅ публікує в канал і пише log", async () => {
  const env = makeEnv({ "draft:abc12345": DRAFT_CARD });
  await worker.fetch(request(callbackUpdate("p:abc12345")), env);

  const copies = used("copyMessage");
  assert(copies.length === 1, `copyMessage викликано ${copies.length} разів`);
  assert(copies[0].body.chat_id === CHANNEL, "копія пішла не в канал");
  assert(copies[0].body.from_chat_id === DRAFTS, "from_chat_id не з чату чернеток");
  assert(copies[0].body.message_id === 42, "скопійовано не ту чернетку");
  assert(!("reply_markup" in copies[0].body), "кнопки не мають потрапити в канал");

  const logged = JSON.parse(await env.NEWSBOT.get("log:abc12345"));
  assert(logged.decision === "pub", "decision має бути pub");
  assert(logged.channel_message_id === 555, "не збережено id поста в каналі");
  assert(logged.score === 8 && logged.rubric === "крипта", "картка не перенеслась у log");

  assert(used("editMessageReplyMarkup").length === 1, "кнопки не замінено на напис");
  assert(used("answerCallbackQuery").length === 1, "не відповіли на callback");
});

await test("чернетка не видаляється", async () => {
  const env = makeEnv({ "draft:abc12345": DRAFT_CARD });
  await worker.fetch(request(callbackUpdate("p:abc12345")), env);
  assert(used("deleteMessage").length === 0, "чернетку видалили — статистика втрачена");
});

await test("подвійне ✅ не дає другий пост у канал", async () => {
  const env = makeEnv({ "draft:abc12345": DRAFT_CARD });
  await worker.fetch(request(callbackUpdate("p:abc12345")), env);
  const afterFirst = used("copyMessage").length;

  calls = [];
  await worker.fetch(request(callbackUpdate("p:abc12345")), env);

  assert(afterFirst === 1, "перше натискання не спрацювало");
  assert(used("copyMessage").length === 0, "повторне натискання опублікувало вдруге");
  assert(used("answerCallbackQuery").length === 1, "на повторне натискання треба відповісти");
});

await test("❌ пише rej і в канал не лізе", async () => {
  const env = makeEnv({ "draft:abc12345": DRAFT_CARD });
  await worker.fetch(request(callbackUpdate("x:abc12345")), env);

  assert(used("copyMessage").length === 0, "відхилення полізло в канал");
  const logged = JSON.parse(await env.NEWSBOT.get("log:abc12345"));
  assert(logged.decision === "rej", "decision має бути rej");
  assert(logged.channel_message_id === null, "у відхиленого не може бути id в каналі");
  assert(used("answerCallbackQuery").length === 1, "не відповіли на callback");
});

await test("✏️ пише retry, а не log", async () => {
  const env = makeEnv({ "draft:abc12345": DRAFT_CARD });
  await worker.fetch(request(callbackUpdate("r:abc12345")), env);

  assert(used("copyMessage").length === 0, "перегенерація полізла в канал");
  assert(await env.NEWSBOT.get("retry:abc12345"), "не записано retry");
  assert((await env.NEWSBOT.get("log:abc12345")) === null, "retry не має писати log");
  assert(used("answerCallbackQuery").length === 1, "не відповіли на callback");
});

await test("збій copyMessage не лишає хибний log", async () => {
  const env = makeEnv({ "draft:abc12345": DRAFT_CARD });
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    const method = String(url).split("/").pop();
    calls.push({ method, body: JSON.parse(init.body) });
    if (method === "copyMessage") return jsonResp({ ok: false, description: "chat not found" });
    return jsonResp({ ok: true, result: true });
  };

  await worker.fetch(request(callbackUpdate("p:abc12345")), env);
  globalThis.fetch = original;

  assert((await env.NEWSBOT.get("log:abc12345")) === null,
    "записано log, хоча пост у канал не потрапив");
  assert(used("answerCallbackQuery").length === 1, "користувач лишився без відповіді");
});

await test("натискання на вже замінену кнопку (noop) не падає", async () => {
  const env = makeEnv();
  const resp = await worker.fetch(request(callbackUpdate("noop")), env);
  assert(resp.status === 200, "має бути 200");
  assert(used("answerCallbackQuery").length === 1, "на noop теж треба відповісти");
  assert(used("copyMessage").length === 0, "noop нічого не публікує");
});

await test("callback без картки draft не валить воркер", async () => {
  const env = makeEnv();
  const resp = await worker.fetch(request(callbackUpdate("p:missing1")), env);
  assert(resp.status === 200, "має бути 200");
  assert(used("answerCallbackQuery").length === 1, "не відповіли на callback");
});

await test("без TELEGRAM_BOT_TOKEN воркер не публікує і каже про це", async () => {
  // Реальна поломка з першого запуску: секрет забули покласти у воркер, усі
  // виклики летіли на /botundefined/, а кнопка крутилась вічно без пояснень.
  const env = makeEnv({ "draft:abc12345": DRAFT_CARD });
  delete env.TELEGRAM_BOT_TOKEN;

  const errors = [];
  const originalError = console.error;
  console.error = (...args) => errors.push(args.join(" "));

  const resp = await worker.fetch(request(callbackUpdate("p:abc12345")), env);
  console.error = originalError;

  assert(resp.status === 200, "має бути 200, щоб Telegram не ретраїв");
  assert(calls.length === 0, "без токена не можна нікуди ходити");
  assert(errors.some((e) => e.includes("TELEGRAM_BOT_TOKEN")),
    "у лог має потрапити зрозуміла причина");
  assert((await env.NEWSBOT.get("log:abc12345")) === null,
    "не можна писати log, якщо в канал нічого не пішло");
});

console.log(`\nпройдено ${passed}, впало ${failed}`);
process.exit(failed === 0 ? 0 : 1);

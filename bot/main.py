"""Оркестратор прогону. Один запуск = кроки 1-9.

Запуск:  python -m bot.main
Сухий:   DRY_RUN=1 python -m bot.main

Апдейти Telegram тут не читаються. Ніде. Вебхук назавжди вимикає getUpdates,
тож усі натискання кнопок обробляє воркер, а не цей скрипт.
"""
from __future__ import annotations

import logging
import sys
import time

from bot import collect, dedupe, docs, generate, kv as kvmod, publish, state as st
from bot.config import DRY_RUN, ConfigError, env, require_all

log = logging.getLogger("bot")

REQUIRED_SECRETS = [
    "TELEGRAM_BOT_TOKEN",
    "DRAFTS_CHAT_ID",
    "GEMINI_API_KEY",
]

# У бойовому прогоні без KV немає сенсу: кнопки не працюватимуть.
# У сухому — KV не потрібен, бо туди все одно нічого не пишеться, а Cloudflare
# у чеклісті налаштовується пізніше за документи. Хай /test-run буде
# доступний одразу, а не після блоку 3.
KV_SECRETS = ["CF_ACCOUNT_ID", "CF_KV_TOKEN"]


RUN_LOG_PATH = "last_run.log"


class _Recorder(logging.Handler):
    """Складає весь лог прогону в пам'ять, щоб потім покласти у файл.

    Логи GitHub Actions доступні лише адміну репозиторію, тож розбирати
    «чому нічого не вийшло» доводилось наосліп. Файл `last_run.log` комітиться
    разом зі `state.json` — і стає видимим будь-кому, хто дивиться репозиторій.

    Секретів у ньому немає за побудовою: значення env-змінних не логуються
    ніде в проєкті, а відповіді Telegram і Gemini токенів не містять.
    """

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(self.format(record))
        except Exception:  # noqa: BLE001 — логер не має права валити прогін
            pass


_recorder = _Recorder()


def setup_logging() -> None:
    # Windows-консоль інакше калічить кирилицю; в Actions це no-op.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S"
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    _recorder.setFormatter(fmt)
    if _recorder not in logging.getLogger().handlers:
        logging.getLogger().addHandler(_recorder)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def write_run_log(outcome: str) -> None:
    """Покласти лог прогону у файл. Ніколи не валить прогін через помилку запису."""
    try:
        with open(RUN_LOG_PATH, "w", encoding="utf-8") as fh:
            fh.write(f"# прогін {st.now_iso()} — {outcome}\n")
            fh.write(f"# {'DRY_RUN' if DRY_RUN else 'бойовий режим'}\n\n")
            fh.write("\n".join(_recorder.lines))
            fh.write("\n")
    except OSError as exc:
        log.warning("не вдалося записати %s: %s", RUN_LOG_PATH, exc)


def alert(exc: Exception) -> None:
    """Сказати в групу чернеток, що прогін не відбувся.

    Інакше падіння німе: у групі просто тиша, і зрозуміти, що бот зламався,
    а не що новин не було, можна лише зазирнувши в GitHub. Один прогін
    у такому стані — це пів дня без каналу.

    Сама розсилка не має права нічого зламати, тому все загорнуте в except.
    """
    if DRY_RUN:
        return
    try:
        first_line = str(exc).strip().split("\n")[0][:250]
        publish.Telegram().notify(
            "⚠️ <b>Прогін зупинено</b>\n\n"
            f"{first_line}\n\n"
            "Подробиці — у файлі <code>last_run.log</code> в репозиторії."
        )
    except Exception:  # noqa: BLE001
        log.warning("не вдалося попередити групу про падіння прогону")


def rank(item: dict) -> float:
    """Ранг кандидата: вага джерела важить більше за свіжість.

    Вага 3 (регулятор, суд, біржа) має обходити вагу 1 (сигнал із ТГ) навіть
    якщо сигнал свіжіший на кілька годин — саме так розставлені пріоритети
    в документі 02.
    """
    weight = int(item.get("weight", 2))
    published = st.parse_iso(item.get("published_at", ""))
    if published:
        age_hours = max(0.0, (st.now() - published).total_seconds() / 3600)
        freshness = max(0.0, 6.0 - age_hours / 6.0)
    else:
        freshness = 1.0
    return weight * 10 + freshness


def pick_candidates(items: list[dict], limit: int,
                    rubric_limits: dict[str, int] | None = None) -> list[dict]:
    """Відібрати до `limit` кандидатів у модель, розподіляючи по рубриках.

    Round-robin, а не просто топ-N за рангом: інакше найбагатша на джерела
    рубрика з'їдає всі слоти й решта не доходить до моделі ніколи.

    Порядок обходу — за спаданням ліміту рубрики. Ліміт у config/sources.txt
    і є вираженням пріоритету: Київ там найбільший, бо це головна рубрика
    каналу. Без цього при чотирьох слотах головна рубрика могла лишитись
    без жодного, хоча джерел у неї найменше саме тому, що місто вузьке.
    """
    if len(items) <= limit:
        return sorted(items, key=rank, reverse=True)

    limits = rubric_limits or {}
    by_rubric: dict[str, list[dict]] = {}
    for item in sorted(items, key=rank, reverse=True):
        by_rubric.setdefault(item.get("rubric", "?"), []).append(item)

    order = sorted(by_rubric, key=lambda r: (-limits.get(r, 0), r))

    picked: list[dict] = []
    while len(picked) < limit and any(by_rubric.values()):
        for rubric in order:
            if len(picked) >= limit:
                break
            queue = by_rubric[rubric]
            if queue:
                picked.append(queue.pop(0))

    log.info(
        "передфільтр: %d айтемів → %d кандидатів у модель, по рубриках: %s",
        len(items), len(picked),
        {r: sum(1 for p in picked if p.get("rubric") == r)
         for r in sorted({p.get("rubric", "?") for p in picked})},
    )
    return picked


def apply_limits(posts: list[dict], total_limit: int,
                 rubric_limits: dict[str, int]) -> list[dict]:
    """Ліміти вирішує КОД, а не модель: модель оцінює кожен айтем ізольовано
    і не знає про конкуренцію за слоти."""
    chosen: list[dict] = []
    used: dict[str, int] = {}
    dropped_rubric = dropped_total = 0

    for post in sorted(posts, key=lambda p: p.get("score", 0), reverse=True):
        if len(chosen) >= total_limit:
            dropped_total += 1
            continue

        rubric = post.get("rubric", "")
        cap = rubric_limits.get(rubric)
        if cap is not None and used.get(rubric, 0) >= cap:
            dropped_rubric += 1
            continue

        used[rubric] = used.get(rubric, 0) + 1
        chosen.append(post)

    log.info(
        "ліміти: %d постів → %d чернеток (відсіяно %d по лімітах рубрик, "
        "%d по загальному ліміту %d). Розподіл: %s",
        len(posts), len(chosen), dropped_rubric, dropped_total, total_limit, used,
    )
    return chosen


def retry_items(retries: list[dict]) -> list[dict]:
    """Картки з KV, які людина попросила переписати, назад у чергу генерації."""
    items: list[dict] = []
    for card in retries:
        raw = card.get("raw_item")
        if not raw:
            log.warning("retry %s без збереженого raw_item — пропускаємо",
                        card.get("draft_id", "?"))
            continue
        raw = dict(raw)
        raw["is_retry"] = True
        items.append(raw)
    if items:
        log.info("на перегенерацію: %d айтемів", len(items))
    return items


def run() -> int:
    setup_logging()
    log.info("=== прогін почався%s ===", " [DRY_RUN]" if DRY_RUN else "")

    require_all(REQUIRED_SECRETS)
    have_kv = all(env(name, required=False) for name in KV_SECRETS)
    if not have_kv:
        if not DRY_RUN:
            require_all(KV_SECRETS)
        log.warning(
            "немає %s — у сухому прогоні працюємо без KV: рішення по кнопках "
            "не зчитуються, картки чернеток не пишуться", ", ".join(KV_SECRETS),
        )

    current = st.load()

    # --- 0. Перевірка зв'язку -------------------------------------------
    # Read-only, нічого не надсилає. Стоїть тут, а не перед кроком 8, щоб
    # зламаний токен не з'ясовувався після витраченої квоти Gemini.
    tg = publish.Telegram()
    if not tg.verify():
        return 1

    # --- 1. Забрати рішення воркера з KV -------------------------------
    kv = None
    retries: list[dict] = []
    if have_kv:
        try:
            kv = kvmod.KV()
            kv.resolve_namespace(cached=current.get("kv_namespace_id", ""))
            current["kv_namespace_id"] = kv.namespace_id
            retries = kvmod.drain(kv, current)
        except ConfigError:
            # У бойовому прогоні без KV немає сенсу — падаємо. У сухому це лише
            # заважає перевірити стиль постів, тому попереджаємо й ідемо далі.
            if not DRY_RUN:
                raise
            log.warning("KV недоступний — сухий прогін триває без нього")
            kv = None

    # --- 2. Конфіг редакції ---------------------------------------------
    # Файли в репозиторії; URL-змінні, якщо задані, перекривають їх.
    prompt_doc = docs.load(
        docs.PROMPT_PATH, env("PROMPT_DOC_URL", required=False), "промпт стилю"
    )
    sources_doc = docs.load(
        docs.SOURCES_PATH, env("SOURCES_DOC_URL", required=False), "джерела"
    )
    config = docs.parse_sources(sources_doc)

    # --- 3. Зібрати матеріал -------------------------------------------
    items = collect.collect_all(config["sources"], current)

    # --- 4. Дедуп -------------------------------------------------------
    fresh = dedupe.filter_new(items, current)

    # --- 5. Передфільтр до ліміту кандидатів ---------------------------
    candidates = retry_items(retries) + pick_candidates(
        fresh, config["candidate_limit"], config["rubric_limits"]
    )
    if not candidates:
        log.info("нових айтемів немає — прогін завершено без чернеток")
        st.prune(current)
        st.save(current)
        return 0

    # --- 6. Генерація ---------------------------------------------------
    recent = st.recent_published(current, limit=30)
    allowed_rubrics = {s["rubric"] for s in config["sources"]} | set(config["rubric_limits"])
    posts = generate.generate(prompt_doc, candidates, recent, allowed_rubrics)

    # Баченими вважаємо лише те, що модель реально подивилась. Айтеми, до яких
    # черга не дійшла (скінчилась денна квота), лишаються непозначеними
    # й повертаються наступного прогону.
    marked = 0
    for item in candidates:
        if item.get("_processed") and item.get("hash") is not None:
            current["seen"].append({"hash": str(item["hash"]), "ts": st.now_iso()})
            marked += 1
    if marked < len(candidates):
        log.info("у seen[] додано %d із %d кандидатів — решта повернеться наступного прогону",
                 marked, len(candidates))

    # --- 7. Ліміти ------------------------------------------------------
    chosen = apply_limits(posts, config["total_limit"], config["rubric_limits"])

    # --- 8. Чернетки в групу + картки в KV ------------------------------
    sent = 0
    for index, post in enumerate(chosen):
        problems = publish.validate_html(post["text"])
        if problems:
            log.error("невалідний HTML, чернетку пропущено: %s",
                      "; ".join(problems[:3]))
            continue

        draft_id = publish.new_draft_id()
        meta = (f"score {post['score']} | {post['rubric']} | "
                f"шаблон {post.get('template', '-')} | {post['source_url'][:60]}")

        if index:
            time.sleep(publish.DRAFT_PAUSE_SEC)

        image = post.get("_item", {}).get("image", "")
        message_id = tg.send_draft(draft_id, post["text"], meta, image)
        if message_id is None:
            # Пост готовий, витрачений виклик моделі — а Telegram його не взяв.
            # Знімаємо позначку «бачений», щоб айтем повернувся наступного
            # прогону, а не зник разом із причиною збою.
            failed_hash = str(post.get("_item", {}).get("hash", ""))
            before = len(current["seen"])
            current["seen"] = [
                r for r in current["seen"] if str(r.get("hash")) != failed_hash
            ]
            log.error(
                "чернетку %s не вдалося надіслати — повертаю айтем у чергу%s",
                draft_id,
                "" if len(current["seen"]) < before else " (хеш не знайдено)",
            )
            continue

        source_item = post.get("_item", {})
        card = {
            "story_key": post["story_key"],
            "score": post["score"],
            "rubric": post["rubric"],
            "topic": post["story_key"],
            "source_url": post["source_url"],
            "ts": st.now_iso(),
            "drafts_message_id": message_id,
            # Потрібне, щоб кнопка «Переписати» мала що переписувати.
            "raw_item": {
                k: source_item.get(k, "")
                for k in ("title", "text", "url", "origin", "rubric", "weight",
                          "image", "source_type", "source", "published_at")
            },
        }
        if kv is not None and not kv.put(f"draft:{draft_id}", card):
            # Чернетка вже в групі, але воркеру не буде що логувати.
            # Краще сказати про це вголос, ніж мовчки втратити рішення.
            log.error("картка draft:%s не записалась у KV — кнопки не спрацюють", draft_id)
        sent += 1

    log.info("надіслано чернеток: %d", sent)

    # --- 9. Стан --------------------------------------------------------
    st.prune(current)
    st.save(current)
    log.info("=== прогін завершено ===")
    return 0


def main() -> int:
    try:
        code = run()
    except ConfigError as exc:
        setup_logging()
        log.error("прогін зупинено: %s", exc)
        print(f"\n!!! {exc}\n", file=sys.stderr)
        write_run_log("ЗУПИНЕНО: немає конфігу або секретів")
        alert(exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        # Навіть несподіване падіння має лишити слід у файлі, інакше
        # розбиратись доведеться знову наосліп.
        setup_logging()
        log.exception("несподіване падіння: %s", exc)
        write_run_log(f"ВПАВ: {type(exc).__name__}")
        raise
    write_run_log("завершено" if code == 0 else f"код виходу {code}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

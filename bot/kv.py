"""Cloudflare KV через REST API — черга рішень між воркером і збирачем.

Хто що пише:
  збирач → draft:<id>    картка чернетки, щоб воркер мав що залогувати
  воркер → log:<id>      рішення людини (pub/rej)
  воркер → retry:<id>    запит на перегенерацію
  збирач ← читає log:* і retry:*, зливає в state.json і ВИДАЛЯЄ їх з KV

Видалення після злиття обов'язкове: без нього наступний прогін поглине ті самі
рішення вдруге і подвоїть статистику.
"""
from __future__ import annotations

import json
import logging

import requests

from bot.config import CF_KV_NAMESPACE, DRY_RUN, ConfigError, env

log = logging.getLogger(__name__)

API = "https://api.cloudflare.com/client/v4"


class KV:
    def __init__(self, namespace_id: str = "") -> None:
        self.account_id = env("CF_ACCOUNT_ID")
        self._token = env("CF_KV_TOKEN")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        })
        self.namespace_id = namespace_id

    # --- службове -------------------------------------------------------

    def _url(self, tail: str) -> str:
        return f"{API}/accounts/{self.account_id}/storage/kv/namespaces/{tail}"

    def _call(self, method: str, url: str, **kwargs) -> dict:
        resp = self.session.request(method, url, timeout=20, **kwargs)
        try:
            data = resp.json()
        except ValueError:
            # Не JSON — майже завжди означає проблему з токеном або акаунтом.
            raise ConfigError(
                f"Cloudflare відповів не-JSON (HTTP {resp.status_code}).\n"
                f"Перевір CF_ACCOUNT_ID і права токена CF_KV_TOKEN "
                f"(потрібен Workers KV Storage: Edit)."
            )
        if not data.get("success", False):
            errors = "; ".join(
                str(e.get("message", e)) for e in data.get("errors", [])
            ) or f"HTTP {resp.status_code}"
            raise ConfigError(f"Cloudflare KV: {errors}")
        return data

    def resolve_namespace(self, cached: str = "") -> str:
        """ID неймспейсу в секретах немає — резолвимо за назвою (NEWSBOT).

        Знайдений id кешується в state.json, тож зайвий запит робиться
        рівно один раз за все життя проєкту.
        """
        if self.namespace_id:
            return self.namespace_id

        override = env("CF_KV_NAMESPACE_ID", required=False)
        if override:
            self.namespace_id = override
            return override

        if cached:
            self.namespace_id = cached
            return cached

        data = self._call(
            "GET",
            f"{API}/accounts/{self.account_id}/storage/kv/namespaces",
            params={"per_page": 100},
        )
        for ns in data.get("result", []):
            if ns.get("title") == CF_KV_NAMESPACE:
                self.namespace_id = ns["id"]
                log.info("KV namespace «%s» знайдено за назвою", CF_KV_NAMESPACE)
                return self.namespace_id

        titles = ", ".join(
            ns.get("title", "?") for ns in data.get("result", [])
        ) or "жодного"
        raise ConfigError(
            f"KV namespace з назвою «{CF_KV_NAMESPACE}» не знайдено.\n"
            f"Наявні неймспейси: {titles}.\n"
            f"Створи його: Cloudflare → Workers & Pages → KV → Create namespace "
            f"→ {CF_KV_NAMESPACE}"
        )

    # --- операції -------------------------------------------------------

    def list_keys(self, prefix: str) -> list[str]:
        keys: list[str] = []
        cursor = ""
        while True:
            params: dict = {"prefix": prefix, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            data = self._call(
                "GET", self._url(f"{self.namespace_id}/keys"), params=params
            )
            keys.extend(k["name"] for k in data.get("result", []))
            cursor = (data.get("result_info") or {}).get("cursor") or ""
            if not cursor:
                break
        return keys

    def get(self, key: str) -> dict | None:
        resp = self.session.get(
            self._url(f"{self.namespace_id}/values/{key}"), timeout=20
        )
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            log.warning("KV get %s → HTTP %s", key, resp.status_code)
            return None
        try:
            return json.loads(resp.text)
        except ValueError:
            log.error("KV %s містить не-JSON, пропускаємо", key)
            return None

    def put(self, key: str, value: dict) -> bool:
        if DRY_RUN:
            print(f"  [DRY_RUN] KV put {key} = {json.dumps(value, ensure_ascii=False)}")
            return True
        # Ендпоінт values приймає сире тіло, а не JSON-обгортку API,
        # тому Content-Type тут окремий.
        resp = self.session.put(
            self._url(f"{self.namespace_id}/values/{key}"),
            data=json.dumps(value, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            timeout=20,
        )
        if resp.status_code != 200:
            log.error("KV put %s → HTTP %s: %s", key, resp.status_code, resp.text[:200])
            return False
        return True

    def delete_bulk(self, keys: list[str]) -> None:
        if not keys:
            return
        if DRY_RUN:
            print(f"  [DRY_RUN] KV delete {len(keys)} ключів: {', '.join(keys[:5])}")
            return
        for start in range(0, len(keys), 10000):
            chunk = keys[start:start + 10000]
            resp = self.session.delete(
                self._url(f"{self.namespace_id}/bulk"),
                data=json.dumps(chunk).encode("utf-8"),
                timeout=30,
            )
            if resp.status_code != 200:
                log.error("KV bulk delete → HTTP %s: %s",
                          resp.status_code, resp.text[:200])
            else:
                log.info("KV: видалено %d оброблених ключів", len(chunk))


def drain(kv: KV, state: dict) -> list[dict]:
    """Крок 1 пайплайну: забрати рішення воркера в state.json.

    Повертає картки чернеток, які людина попросила перегенерувати.
    """
    from bot.state import now_iso

    log_keys = kv.list_keys("log:")
    retry_keys = kv.list_keys("retry:")

    # Захист від подвійного злиття: воркер міг записати log повторно
    # (Telegram ретраїть вебхуки), а ми вже врахували це минулого прогону.
    known_ids = {r.get("draft_id") for r in state["stats"] if r.get("draft_id")}
    processed: list[str] = []
    merged = 0

    for key in log_keys:
        record = kv.get(key)
        processed.append(key)
        if not record:
            continue

        draft_id = key.split(":", 1)[1]
        processed.append(f"draft:{draft_id}")

        if draft_id in known_ids:
            continue

        decision = record.get("decision", "rej")
        state["stats"].append({
            "score": record.get("score", 0),
            "rubric": record.get("rubric", ""),
            "decision": decision,
            "ts": record.get("ts") or now_iso(),
            "draft_id": draft_id,
        })
        merged += 1

        if decision == "pub" and record.get("channel_message_id"):
            state["published"].append({
                "message_id": record["channel_message_id"],
                "story_key": record.get("story_key", ""),
                "rubric": record.get("rubric", ""),
                "topic": record.get("topic") or record.get("story_key", ""),
                "ts": record.get("ts") or now_iso(),
            })

    retries: list[dict] = []
    for key in retry_keys:
        draft_id = key.split(":", 1)[1]
        processed.append(key)
        card = kv.get(f"draft:{draft_id}")
        if card:
            card["draft_id"] = draft_id
            retries.append(card)
        else:
            log.warning("retry:%s без картки draft — пропускаємо", draft_id)

    log.info(
        "KV: злито %d нових рішень із %d ключів log, запитів на перегенерацію: %d",
        merged, len(log_keys), len(retries),
    )

    # draft-картки, які щойно пішли в перегенерацію, не чіпаємо —
    # вони знадобляться, поки не з'явиться нова чернетка.
    keep = {f"draft:{r['draft_id']}" for r in retries}
    kv.delete_bulk([k for k in dict.fromkeys(processed) if k not in keep])
    return retries

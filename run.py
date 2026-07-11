"""Один прогон: сходить на hh, отфильтровать, прислать новое в Telegram.

Запускается по расписанию через GitHub Actions. Ничего не помнит между запусками,
кроме файла seen.json, который коммитится обратно в репозиторий.
"""
import html
import json
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import config
import filters

logging.basicConfig(format="%(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("run")

HH_BASE = "https://api.hh.ru"
TG_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"
SEEN_PATH = Path(__file__).parent / "seen.json"


# ---------------------------------------------------------------------------
# Память: id вакансии -> дата, когда её показали
# ---------------------------------------------------------------------------
def load_seen() -> dict[str, str]:
    if not SEEN_PATH.exists():
        return {}
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("seen.json повреждён, начинаю с чистого листа")
        return {}


def save_seen(seen: dict[str, str]) -> None:
    """Сохраняет, попутно забывая всё старше FORGET_AFTER_DAYS."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.FORGET_AFTER_DAYS)
    fresh = {}
    for vid, stamp in seen.items():
        try:
            if datetime.fromisoformat(stamp) >= cutoff:
                fresh[vid] = stamp
        except ValueError:
            continue
    SEEN_PATH.write_text(
        json.dumps(fresh, ensure_ascii=False, indent=0, sort_keys=True),
        encoding="utf-8",
    )
    log.info("seen.json: %d записей (было %d)", len(fresh), len(seen))


# ---------------------------------------------------------------------------
# hh.ru
# ---------------------------------------------------------------------------
def hh_headers() -> dict:
    h = {"User-Agent": config.HH_USER_AGENT}
    if config.HH_TOKEN:
        h["Authorization"] = f"Bearer {config.HH_TOKEN}"
    return h


def hh_get(url: str, params: dict | None = None, _retry: int = 0) -> dict:
    r = requests.get(url, params=params, headers=hh_headers(), timeout=25)
    if r.status_code == 403:
        raise PermissionError(
            "hh.ru вернул 403. Возможно, нужен токен приложения: "
            "зарегистрируй его на dev.hh.ru/admin и добавь в секреты как HH_TOKEN."
        )
    if r.status_code == 429 and _retry < 3:
        time.sleep(20 * (_retry + 1))
        return hh_get(url, params, _retry + 1)
    r.raise_for_status()
    return r.json()


def hh_search(text: str) -> list[dict]:
    items: list[dict] = []
    for page in range(config.MAX_PAGES):
        data = hh_get(
            f"{HH_BASE}/vacancies",
            {
                "text": text,
                "area": config.AREA,
                "schedule": config.SCHEDULE,
                "experience": config.EXPERIENCE,
                "period": config.PERIOD_DAYS,
                "per_page": 100,
                "page": page,
                "order_by": "publication_time",
            },
        )
        items.extend(data.get("items", []))
        if page >= data.get("pages", 1) - 1:
            break
        time.sleep(0.4)
    log.info("«%s» — найдено %d", text, len(items))
    return items


def hh_detail(vacancy_id: str) -> dict:
    time.sleep(0.3)
    return hh_get(f"{HH_BASE}/vacancies/{vacancy_id}")


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def tg_send(text: str) -> None:
    r = requests.post(
        f"{TG_BASE}/sendMessage",
        json={
            "chat_id": config.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )
    if not r.ok:
        log.error("Telegram отказал: %s", r.text)


def format_salary(salary: dict | None) -> str:
    if not salary:
        return "з/п не указана"
    lo, hi = salary.get("from"), salary.get("to")
    cur = (salary.get("currency") or "RUR").replace("RUR", "₽")
    if lo and hi:
        return f"{lo:,} – {hi:,} {cur}".replace(",", " ")
    if lo:
        return f"от {lo:,} {cur}".replace(",", " ")
    if hi:
        return f"до {hi:,} {cur}".replace(",", " ")
    return "з/п не указана"


def render(vacancy: dict, verdict: str, reasons: list[str]) -> str:
    e = html.escape
    employer = (vacancy.get("employer") or {}).get("name") or "работодатель не указан"
    lines = [
        f"<b>{e(vacancy['name'])}</b>",
        e(employer),
        f"💰 {e(format_salary(vacancy.get('salary')))}",
        f"🔗 {vacancy['alternate_url']}",
    ]
    if verdict == filters.Verdict.WARN:
        lines += ["", "⚠️ <b>Проверь внимательно:</b>"]
        lines += [f"• {e(r)}" for r in reasons[:3]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def main() -> int:
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.error("Не заданы TELEGRAM_TOKEN и TELEGRAM_CHAT_ID в секретах репозитория")
        return 1

    seen = load_seen()
    now = datetime.now(timezone.utc).isoformat()
    sent = kept = dropped = 0

    try:
        for query in config.QUERIES:
            try:
                items = hh_search(query)
            except PermissionError as exc:
                tg_send(f"🚫 {html.escape(str(exc))}")
                return 1
            except requests.RequestException:
                log.exception("Сетевая ошибка на запросе «%s», пропускаю", query)
                continue

            for item in items:
                vid = item["id"]
                if vid in seen:
                    continue

                # Дешёвый отсев по названию — до похода за полным описанием.
                if filters.is_hard_excluded(item.get("name") or ""):
                    seen[vid] = now
                    dropped += 1
                    continue

                try:
                    detail = hh_detail(vid)
                except (requests.RequestException, PermissionError):
                    continue

                verdict, reasons = filters.classify(detail, detail.get("description", ""))

                if verdict == filters.Verdict.DROP:
                    seen[vid] = now
                    dropped += 1
                    log.info("DROP %s — %s", item["name"], "; ".join(reasons[:2]))
                    continue

                kept += 1
                if sent >= config.MAX_SEND_PER_RUN:
                    # Не помечаем как показанную — придёт в следующий прогон.
                    continue

                tg_send(render(detail, verdict, reasons))
                seen[vid] = now
                sent += 1
                time.sleep(0.5)
    finally:
        # Сохраняем даже при аварии, чтобы не показывать одно и то же дважды.
        save_seen(seen)

    if sent or dropped:
        tail = f" Ещё {kept - sent} придут в следующий прогон." if kept > sent else ""
        tg_send(f"✅ Прогон завершён. Отправлено: {sent}. Отсеяно: {dropped}.{tail}")
    else:
        log.info("Новых вакансий нет — молчу")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Один прогон: обойти все включённые источники, отфильтровать, прислать новое.
Запускается по расписанию через GitHub Actions."""
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
from models import Vacancy
from sources.hh import HHSource, SourceUnavailable
from sources.telegram_channels import TelegramChannelsSource
from sources.trudvsem import TrudvsemSource

logging.basicConfig(format="%(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger("run")

TG_BASE = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}"
SEEN_PATH = Path(__file__).parent / "seen.json"

# Реестр источников: имя -> класс.
REGISTRY = {
    "telegram": TelegramChannelsSource,
    "trudvsem": TrudvsemSource,
    "hh": HHSource,
}


# ---------------------------------------------------------------------------
# Память
# ---------------------------------------------------------------------------
def load_seen() -> dict[str, str]:
    if not SEEN_PATH.exists():
        return {}
    try:
        return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("seen.json повреждён, начинаю заново")
        return {}


def save_seen(seen: dict[str, str]) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.FORGET_AFTER_DAYS)
    fresh = {}
    for uid, stamp in seen.items():
        try:
            if datetime.fromisoformat(stamp) >= cutoff:
                fresh[uid] = stamp
        except ValueError:
            continue
    SEEN_PATH.write_text(
        json.dumps(fresh, ensure_ascii=False, indent=0, sort_keys=True),
        encoding="utf-8",
    )
    log.info("seen.json: %d записей", len(fresh))


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
def tg_send(text: str) -> None:
    try:
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
    except requests.RequestException as exc:
        log.error("Telegram недоступен: %s", exc)


def format_salary(v: Vacancy) -> str:
    lo, hi = v.salary_from, v.salary_to
    cur = (v.currency or "RUR").replace("RUR", "₽")
    if lo and hi:
        return f"{lo:,} – {hi:,} {cur}".replace(",", " ")
    if lo:
        return f"от {lo:,} {cur}".replace(",", " ")
    if hi:
        return f"до {hi:,} {cur}".replace(",", " ")
    return "з/п не указана"


def render(v: Vacancy, verdict: str, reasons: list[str]) -> str:
    e = html.escape
    employer_line = e(v.employer) if v.employer else ("из канала" if v.source == "tg" else "работодатель не указан")
    lines = [
        f"<b>{e(v.title)}</b>",
        f"{employer_line} · <i>{e(v.source_label)}</i>",
        f"💰 {e(format_salary(v))}",
        f"🔗 {v.url}",
    ]
    if verdict == filters.Verdict.WARN:
        lines += ["", "⚠️ <b>Проверь внимательно:</b>"]
        lines += [f"• {e(r)}" for r in reasons[:3]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
def gather() -> tuple[list[Vacancy], list[str]]:
    """Собирает вакансии со всех включённых источников.
    Возвращает (вакансии, список заметок о недоступных источниках)."""
    all_vacs: list[Vacancy] = []
    notes: list[str] = []

    for name, enabled in config.SOURCES.items():
        if not enabled or name not in REGISTRY:
            continue
        source = REGISTRY[name]()
        try:
            vacs = source.fetch(config.QUERIES)
            all_vacs.extend(vacs)
            log.info("Источник %s: %d вакансий", name, len(vacs))
        except SourceUnavailable as exc:
            notes.append(str(exc))
            log.warning("Источник %s недоступен: %s", name, exc)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"Источник {name} упал с ошибкой: {exc}")
            log.exception("Источник %s упал", name)

    return all_vacs, notes


def main() -> int:
    if not config.TELEGRAM_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.error("Не заданы TELEGRAM_TOKEN и TELEGRAM_CHAT_ID")
        return 1

    seen = load_seen()
    now = datetime.now(timezone.utc).isoformat()
    sent = kept = dropped = 0

    try:
        vacancies, notes = gather()

        # Если вообще ни одного источника — сообщаем и выходим с ошибкой.
        if not vacancies and notes:
            for n in notes:
                tg_send(f"⚠️ {html.escape(n)}")
            tg_send("❌ Ни один источник не отдал вакансии в этот прогон.")
            return 1

        for v in vacancies:
            if v.uid in seen:
                continue

            verdict, reasons = filters.classify(v)

            if verdict == filters.Verdict.DROP:
                seen[v.uid] = now
                dropped += 1
                log.info("DROP [%s] %s — %s", v.source, v.title, "; ".join(reasons[:2]))
                continue

            kept += 1
            if sent >= config.MAX_SEND_PER_RUN:
                continue  # не помечаем — придёт в следующий прогон

            tg_send(render(v, verdict, reasons))
            seen[v.uid] = now
            sent += 1
            time.sleep(0.5)
    finally:
        save_seen(seen)

    # Заметки о недоступных источниках — тихо, одной строкой, не пугая.
    for n in notes:
        log.info("Заметка: %s", n)

    if sent or dropped:
        tail = f" Ещё {kept - sent} придут в следующий прогон." if kept > sent else ""
        note_line = f"\nℹ️ {notes[0]}" if notes else ""
        tg_send(f"✅ Прогон завершён. Отправлено: {sent}. Отсеяно: {dropped}.{tail}{note_line}")
    elif not notes:
        log.info("Новых вакансий нет")
    else:
        tg_send(f"ℹ️ Новых вакансий нет. {html.escape(notes[0])}")

    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Источник: публичные Telegram-каналы с вакансиями.

Читаем веб-версию канала t.me/s/<канал> — это открытый HTML, доступный
с любого IP, без токенов и авторизации. Каждый пост канала — потенциальная
вакансия. Оставляем только те, что упоминают наши ключевые слова, остальное
(и мусор) отсеет общий антифрод-фильтр.

Пост в Telegram — свободный текст, поэтому:
- title  = первая строка поста
- description = весь текст (по нему работает антифрод)
- employer, salary — обычно не вытащить, оставляем пустыми
"""
import hashlib
import logging
import re
import time

import requests
from bs4 import BeautifulSoup

import config
from models import Vacancy
from sources.base import Source

log = logging.getLogger("telegram")

# Простая эвристика зарплаты: «от 50000», «50 000 руб», «50к», «$1500».
SALARY_RE = re.compile(
    r"(?:от\s*)?(\d[\d\s]{3,})\s*(?:руб|₽|р\.)|(\d{2,3})\s*к\b|\$\s*(\d{3,5})",
    re.I,
)


def _extract_salary(text: str) -> int | None:
    m = SALARY_RE.search(text or "")
    if not m:
        return None
    if m.group(1):
        return int(re.sub(r"\s", "", m.group(1)))
    if m.group(2):
        return int(m.group(2)) * 1000
    if m.group(3):
        return int(m.group(3)) * 90  # грубый пересчёт $ в ₽, только для порядка
    return None


class TelegramChannelsSource(Source):
    name = "telegram"

    def _is_relevant(self, text: str) -> bool:
        low = text.lower()
        return any(kw in low for kw in config.TELEGRAM_KEYWORDS)

    def _fetch_channel(self, session: requests.Session, channel: str) -> list[Vacancy]:
        url = f"https://t.me/s/{channel}"
        r = session.get(url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        out: list[Vacancy] = []
        for msg in soup.select(".tgme_widget_message"):
            post_id = msg.get("data-post")  # вида "channel/1234"
            text_el = msg.select_one(".tgme_widget_message_text")
            if not text_el or not post_id:
                continue

            text = text_el.get_text("\n", strip=True)
            if len(text) < 40 or not self._is_relevant(text):
                continue

            first_line = next((ln.strip() for ln in text.split("\n") if ln.strip()), "вакансия")
            title = first_line[:90]

            out.append(
                Vacancy(
                    source="tg",
                    id=post_id,
                    title=title,
                    employer="",  # в Telegram работодателя обычно нет в структуре
                    url=f"https://t.me/{post_id}",
                    salary_from=_extract_salary(text),
                    schedule="удалённо",
                    description=text,
                )
            )
        return out

    def fetch(self, queries: list[str]) -> list[Vacancy]:
        found: dict[str, Vacancy] = {}
        failed: list[str] = []

        with requests.Session() as session:
            session.headers["User-Agent"] = config.HH_USER_AGENT
            for channel in config.TELEGRAM_CHANNELS:
                try:
                    posts = self._fetch_channel(session, channel)
                    for v in posts:
                        # Дедуп по тексту: один и тот же пост часто кросс-постят
                        # в разных каналах — считаем уникальность по началу текста.
                        key = hashlib.md5(v.description[:120].encode()).hexdigest()
                        found.setdefault(key, v)
                    log.info("Канал @%s: %d релевантных постов", channel, len(posts))
                except requests.RequestException as exc:
                    failed.append(channel)
                    log.warning("Канал @%s не прочитался: %s", channel, exc)
                time.sleep(0.5)

        if failed:
            # Пробрасываем как заметку через атрибут — run.py её подхватит.
            self.failed_channels = failed
        log.info("Telegram: собрано %d вакансий из каналов", len(found))
        return list(found.values())

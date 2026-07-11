"""Источник: hh.ru. Внимание: с конца 2025 hh может отдавать 403 на запросы
с серверных IP (включая GitHub Actions). Адаптер это ловит и не роняет прогон —
просто возвращает пустой список и сообщает причину через исключение SourceUnavailable.
"""
import logging
import time

import requests

import config
from models import Vacancy
from sources.base import Source

log = logging.getLogger("hh")
BASE = "https://api.hh.ru/vacancies"


class SourceUnavailable(RuntimeError):
    """Источник временно недоступен (например, hh отдаёт 403). Не фатально."""


class HHSource(Source):
    name = "hh"

    def _headers(self) -> dict:
        h = {"User-Agent": config.HH_USER_AGENT}
        if config.HH_TOKEN:
            h["Authorization"] = f"Bearer {config.HH_TOKEN}"
        return h

    def _detail(self, session: requests.Session, vid: str) -> str:
        try:
            time.sleep(0.3)
            r = session.get(f"{BASE}/{vid}", headers=self._headers(), timeout=20)
            if r.ok:
                return r.json().get("description", "") or ""
        except requests.RequestException:
            pass
        return ""

    def _search_one(self, session: requests.Session, text: str) -> list[Vacancy]:
        out: list[Vacancy] = []
        for page in range(config.HH_MAX_PAGES):
            params = {
                "text": text,
                "area": config.HH_AREA,
                "schedule": config.HH_SCHEDULE,
                "experience": config.HH_EXPERIENCE,
                "period": config.PERIOD_DAYS,
                "per_page": 100,
                "page": page,
                "order_by": "publication_time",
            }
            r = session.get(BASE, params=params, headers=self._headers(), timeout=25)
            if r.status_code == 403:
                raise SourceUnavailable(
                    "hh.ru отдаёт 403 — скорее всего блокирует серверный IP GitHub. "
                    "Это ожидаемо; Trudvsem работает независимо. Чтобы убрать это "
                    "сообщение, поставь SOURCES['hh'] = False в config.py."
                )
            if r.status_code == 429:
                time.sleep(20)
                continue
            r.raise_for_status()
            data = r.json()

            for item in data.get("items", []):
                emp = item.get("employer") or {}
                salary = item.get("salary") or {}
                out.append(
                    Vacancy(
                        source="hh",
                        id=str(item["id"]),
                        title=item.get("name") or "без названия",
                        employer=emp.get("name") or "",
                        url=item.get("alternate_url") or "",
                        salary_from=salary.get("from"),
                        salary_to=salary.get("to"),
                        currency=salary.get("currency") or "RUR",
                        schedule="удалённо",
                        description=(item.get("snippet") or {}).get("responsibility") or "",
                        employer_verified=emp.get("trusted"),
                        is_anonymous=(item.get("type") or {}).get("id") == "anonymous",
                        has_address=bool(item.get("address")),
                    )
                )
            if page >= data.get("pages", 1) - 1:
                break
            time.sleep(0.4)
        return out

    def fetch(self, queries: list[str]) -> list[Vacancy]:
        found: dict[str, Vacancy] = {}
        with requests.Session() as session:
            for q in queries:
                try:
                    for v in self._search_one(session, q):
                        found[v.uid] = v
                except SourceUnavailable:
                    raise  # пробрасываем — прогон пометит hh как недоступный
                except requests.RequestException as exc:
                    log.warning("hh: запрос «%s» не прошёл: %s", q, exc)
                    continue

            # Догружаем описания только для непустого набора — экономим запросы.
            for v in found.values():
                if not v.description:
                    v.description = self._detail(session, v.id)

        log.info("hh: собрано %d вакансий", len(found))
        return list(found.values())

"""Источник: Работа России (trudvsem.ru). Открытый государственный API,
без токенов и авторизации. Документация: trudvsem.ru/opendata/apidesc

Структура ответа (по документации):
{
  "status": "200",
  "meta": {"total": N, "limit": ..., "offset": ...},
  "results": {"vacancies": [{"vacancy": {...}}, ...]}
}
Парсим защищённо: любых полей может не оказаться.
"""
import logging
import time

import requests

import config
from models import Vacancy
from sources.base import Source

log = logging.getLogger("trudvsem")
BASE = "http://opendata.trudvsem.ru/api/v1/vacancies"


def _num(value) -> int | None:
    try:
        n = int(float(value))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def _looks_remote(schedule: str) -> bool:
    low = (schedule or "").lower()
    return any(word in low for word in config.TRUDVSEM_REMOTE_WORDS)


class TrudvsemSource(Source):
    name = "trudvsem"

    def _search_one(self, session: requests.Session, text: str) -> list[Vacancy]:
        out: list[Vacancy] = []
        for page in range(config.TRUDVSEM_MAX_PAGES):
            params = {"text": text, "limit": 100, "offset": page}
            r = session.get(BASE, params=params, timeout=25)
            if r.status_code == 404:
                break  # у trudvsem пустая страница иногда отдаётся как 404
            r.raise_for_status()
            data = r.json()

            vacancies = (
                (data.get("results") or {}).get("vacancies")
                if isinstance(data.get("results"), dict)
                else None
            ) or []

            if not vacancies:
                break

            for wrapper in vacancies:
                vac = wrapper.get("vacancy") if isinstance(wrapper, dict) else None
                if not vac:
                    continue

                schedule = vac.get("schedule") or ""
                # Госпортал завален офисными и вахтовыми вакансиями —
                # оставляем только удалёнку, отбирая по тексту графика.
                if not _looks_remote(schedule):
                    continue

                company = vac.get("company") or {}
                requirement = vac.get("requirement") or {}
                duty = vac.get("duty") or ""

                out.append(
                    Vacancy(
                        source="trudvsem",
                        id=str(vac.get("id") or vac.get("vac_url") or id(wrapper)),
                        title=vac.get("job-name") or "без названия",
                        employer=company.get("name") or "",
                        url=vac.get("vac_url") or "https://trudvsem.ru",
                        salary_from=_num(vac.get("salary_min")),
                        salary_to=_num(vac.get("salary_max")),
                        currency="RUR",
                        schedule=schedule,
                        description=f"{duty} {requirement.get('education', '')} "
                                    f"{requirement.get('experience', '')}",
                        # Госпортал: работодатели проходят проверку по ИНН/ОГРН,
                        # так что считаем их верифицированными по умолчанию.
                        employer_verified=bool(company.get("inn") or company.get("ogrn")) or None,
                        is_anonymous=not company.get("name"),
                        has_address=bool(vac.get("addresses")),
                    )
                )

            if len(vacancies) < 100:
                break
            time.sleep(0.3)
        return out

    def fetch(self, queries: list[str]) -> list[Vacancy]:
        found: dict[str, Vacancy] = {}
        with requests.Session() as session:
            session.headers["User-Agent"] = config.HH_USER_AGENT
            for q in queries:
                try:
                    for v in self._search_one(session, q):
                        found[v.uid] = v  # дедуп внутри источника
                except requests.RequestException as exc:
                    log.warning("Trudvsem: запрос «%s» не прошёл: %s", q, exc)
                    continue
        log.info("Trudvsem: собрано %d удалённых вакансий", len(found))
        return list(found.values())

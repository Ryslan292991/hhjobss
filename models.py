"""Единая форма вакансии. Каждый источник приводит свои данные к ней,
дальше фильтр и отправка работают одинаково, не зная, откуда вакансия."""
from dataclasses import dataclass


@dataclass
class Vacancy:
    source: str                       # "hh" | "trudvsem" — откуда пришла
    id: str                           # id внутри источника
    title: str                        # название должности
    employer: str                     # работодатель
    url: str                          # ссылка на вакансию
    salary_from: int | None = None
    salary_to: int | None = None
    currency: str = "RUR"
    schedule: str = ""                # график: удалёнка/офис и т.п., как отдал источник
    description: str = ""             # текст вакансии (для антифрода)

    # Сигналы доверия — не у всех источников есть, поэтому Optional.
    employer_verified: bool | None = None  # работодатель верифицирован площадкой
    is_anonymous: bool = False              # анонимная вакансия
    has_address: bool = False               # указан ли физический адрес

    @property
    def uid(self) -> str:
        """Глобально уникальный id: источник + локальный id.
        Нужен, чтобы вакансии с одинаковым id из разных источников не путались."""
        return f"{self.source}:{self.id}"

    @property
    def source_label(self) -> str:
        return {"hh": "hh.ru", "trudvsem": "Работа России"}.get(self.source, self.source)

"""Базовый интерфейс источника. Каждый сайт наследуется и реализует fetch()."""
from models import Vacancy


class Source:
    name = "base"

    def fetch(self, queries: list[str]) -> list[Vacancy]:
        """Ищет вакансии по списку запросов, возвращает нормализованные Vacancy.
        Может кинуть исключение — вызывающий код это ловит и продолжает
        с другими источниками."""
        raise NotImplementedError

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import gspread
import requests
from google.oauth2.service_account import Credentials

API_BASE = "https://api.hh.ru"
LOGGER = logging.getLogger("finved_hh_parser")


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = html.unescape(value)
    value = re.sub(r"<[^>]+>", " ", value)
    value = value.replace("ё", "е").lower()
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def contains_phrase(text: str, phrase: str) -> bool:
    phrase_n = normalize_text(phrase)
    if not phrase_n:
        return False
    if re.fullmatch(r"[a-zа-я0-9\s-]+", phrase_n):
        return re.search(rf"(?<!\w){re.escape(phrase_n)}(?!\w)", text, flags=re.I) is not None
    return phrase_n in text


def find_phrases(text: str, phrases: Iterable[str]) -> list[str]:
    return [p for p in phrases if contains_phrase(text, p)]


def safe_get(obj: dict[str, Any] | None, *keys: str, default: Any = "") -> Any:
    cur: Any = obj or {}
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
        if cur is None:
            return default
    return cur


class HHClient:
    def __init__(
        self,
        user_agent: str,
        access_token: str,
        timeout: int = 60,
        retries: int = 4,
        pause: float = 0.35,
    ) -> None:
        if not access_token:
            raise RuntimeError("Не задан HH_ACCESS_TOKEN")

        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": user_agent,
                "HH-User-Agent": user_agent,
                "Accept": "application/json",
                "Authorization": f"Bearer {access_token}",
            }
        )
        self.timeout = timeout
        self.retries = retries
        self.pause = pause

    def get_json(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{API_BASE}{endpoint}"
        last_error: Exception | None = None

        for attempt in range(self.retries):
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
                if response.status_code == 429:
                    wait = min(2 ** attempt, 30)
                    LOGGER.warning("Лимит HH. Пауза %s сек.", wait)
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                time.sleep(self.pause)
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    wait = min(2 ** attempt, 10)
                    LOGGER.warning("Ошибка HH: %s. Повтор через %s сек.", exc, wait)
                    time.sleep(wait)

        raise RuntimeError(f"Не удалось получить данные из {url}: {last_error}")

    def search_vacancies(self, query: str, config: dict[str, Any]) -> Iterable[dict[str, Any]]:
        params: dict[str, Any] = {
            "text": query,
            "area": config.get("area", 113),
            "period": config.get("period_days", 7),
            "per_page": min(int(config.get("per_page", 100)), 100),
            "order_by": config.get("order_by", "publication_time"),
            "page": 0,
        }
        if config.get("search_field"):
            params["search_field"] = config["search_field"]

        max_pages = int(config.get("max_pages_per_query", 10))
        page = 0
        while page < max_pages:
            params["page"] = page
            data = self.get_json("/vacancies", params=params)
            items = data.get("items", [])
            for item in items:
                yield item
            pages = int(data.get("pages", 0))
            page += 1
            if not items or page >= pages:
                break

    def get_vacancy(self, vacancy_id: str) -> dict[str, Any]:
        return self.get_json(f"/vacancies/{vacancy_id}")


def count_hits(text: str, phrases: list[str]) -> tuple[int, list[str]]:
    hits = find_phrases(text, phrases)
    return len(hits), hits


def score_vacancy(vacancy: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    title = normalize_text(vacancy.get("name"))
    description = normalize_text(vacancy.get("description"))
    key_skills = " ".join(normalize_text(s.get("name")) for s in vacancy.get("key_skills", []))
    employer = normalize_text(safe_get(vacancy, "employer", "name"))
    full_text = " ".join([title, description, key_skills, employer])

    # 1) Жесткий отсев по названию
    title_stop_hits = find_phrases(title, config.get("hard_exclude_title", []))
    if title_stop_hits:
        return {
            "score": 0,
            "category": "Исключено",
            "reason": f"Нецелевая должность: {', '.join(title_stop_hits)}",
            "positive_hits": "",
            "negative_hits": ", ".join(title_stop_hits),
            "core_hits": "",
            "hard_excluded": True,
        }

    # 2) Конкуренты / аутсорс-финансы как собственный бизнес
    competitor_hits = find_phrases(full_text, config.get("competitor_phrases", []))
    if len(competitor_hits) >= int(config.get("competitor_min_hits", 2)):
        return {
            "score": 0,
            "category": "Исключено",
            "reason": "Похоже на конкурента / финансовый аутсорс",
            "positive_hits": "",
            "negative_hits": ", ".join(competitor_hits),
            "core_hits": "",
            "hard_excluded": True,
        }

    score = 0
    pos_details: list[str] = []
    neg_details: list[str] = []

    core_hits = find_phrases(full_text, config.get("core_management_phrases", []))
    build_hits = find_phrases(full_text, config.get("build_from_scratch_phrases", []))
    owner_hits = find_phrases(full_text, config.get("owner_phrases", []))

    score += min(len(core_hits) * int(config.get("core_points_per_hit", 7)), int(config.get("core_max_points", 42)))
    score += min(len(build_hits) * int(config.get("build_points_per_hit", 8)), int(config.get("build_max_points", 24)))
    score += min(len(owner_hits) * int(config.get("owner_points_per_hit", 4)), int(config.get("owner_max_points", 8)))

    if core_hits:
        pos_details.append("Управленка: " + ", ".join(core_hits))
    if build_hits:
        pos_details.append("Построение/изменения: " + ", ".join(build_hits))
    if owner_hits:
        pos_details.append("Работа с собственником: " + ", ".join(owner_hits))

    for rule in config.get("positive_rules", []):
        scope = rule.get("scope", "all")
        source = title if scope == "title" else description if scope == "description" else full_text
        hits = find_phrases(source, rule.get("phrases", []))
        if hits:
            pts = int(rule.get("points", 0))
            mode = rule.get("mode", "once")
            add = pts if mode == "once" else pts * len(hits)
            add = min(add, int(rule.get("max_points", add)))
            score += add
            pos_details.append(f"{rule.get('label','+')} +{add}: {', '.join(hits)}")

    specialized_count = 0
    accounting_count = 0
    for rule in config.get("negative_rules", []):
        scope = rule.get("scope", "all")
        source = title if scope == "title" else description if scope == "description" else full_text
        hits = find_phrases(source, rule.get("phrases", []))
        if not hits:
            continue
        pts = abs(int(rule.get("points", 0)))
        mode = rule.get("mode", "once")
        sub = pts if mode == "once" else pts * len(hits)
        sub = min(sub, int(rule.get("max_points", sub)))
        score -= sub
        neg_details.append(f"{rule.get('label','-')} -{sub}: {', '.join(hits)}")
        if rule.get("group") == "accounting":
            accounting_count += len(hits)
        if rule.get("group") == "specialized":
            specialized_count += len(hits)

    # 3) Доминирование бухгалтерии / узкого CFO-функционала
    min_core_for_mixed = int(config.get("min_core_hits_for_mixed_roles", 4))
    if accounting_count >= int(config.get("hard_accounting_hit_limit", 3)) and len(core_hits) < min_core_for_mixed:
        return {
            "score": 0,
            "category": "Исключено",
            "reason": "Доминирует бухгалтерский/налоговый функционал",
            "positive_hits": "; ".join(pos_details),
            "negative_hits": "; ".join(neg_details),
            "core_hits": ", ".join(core_hits),
            "hard_excluded": True,
        }

    if specialized_count >= int(config.get("hard_specialized_hit_limit", 4)) and len(core_hits) < min_core_for_mixed:
        return {
            "score": 0,
            "category": "Исключено",
            "reason": "Доминирует узкоспециализированный функционал (банки/ВЭД/инвестиции/регуляторика)",
            "positive_hits": "; ".join(pos_details),
            "negative_hits": "; ".join(neg_details),
            "core_hits": ", ".join(core_hits),
            "hard_excluded": True,
        }

    score = max(0, min(100, score))
    target_threshold = int(config.get("target_threshold", 58))
    review_threshold = int(config.get("review_threshold", 42))

    if score >= target_threshold and len(core_hits) >= int(config.get("min_core_hits_target", 3)):
        category = "Целевой лид"
        reason = "Основная потребность — управленческий учет / финансовое управление"
    elif score >= review_threshold and len(core_hits) >= int(config.get("min_core_hits_review", 2)):
        category = "Ручная проверка"
        reason = "Есть сильные признаки управленки, но присутствуют смешанные функции"
    else:
        category = "Исключено"
        reason = "Недостаточно признаков целевой управленческой функции"

    return {
        "score": score,
        "category": category,
        "reason": reason,
        "positive_hits": "; ".join(pos_details),
        "negative_hits": "; ".join(neg_details),
        "core_hits": ", ".join(core_hits),
        "hard_excluded": category == "Исключено",
    }


def salary_text(salary: dict[str, Any] | None) -> str:
    if not salary:
        return "Не указана"
    f = salary.get("from")
    t = salary.get("to")
    cur = salary.get("currency") or ""
    gross = "до вычета налогов" if salary.get("gross") else "на руки"
    if f and t:
        return f"{f:,}–{t:,} {cur}, {gross}".replace(",", " ")
    if f:
        return f"от {f:,} {cur}, {gross}".replace(",", " ")
    if t:
        return f"до {t:,} {cur}, {gross}".replace(",", " ")
    return "Не указана"


def vacancy_to_row(vacancy: dict[str, Any], scoring: dict[str, Any], collected_at: str) -> list[Any]:
    employer = vacancy.get("employer") or {}
    address = vacancy.get("address") or {}
    return [
        vacancy.get("id", ""),
        collected_at,
        vacancy.get("published_at", ""),
        employer.get("name", ""),
        vacancy.get("name", ""),
        salary_text(vacancy.get("salary")),
        safe_get(vacancy, "area", "name"),
        safe_get(vacancy, "experience", "name"),
        ", ".join(x.get("name", "") for x in vacancy.get("work_format", [])),
        scoring["category"],
        scoring["score"],
        scoring["reason"],
        scoring["core_hits"],
        scoring["positive_hits"],
        scoring["negative_hits"],
        normalize_text(vacancy.get("description")),
        vacancy.get("alternate_url", ""),
        address.get("raw", ""),
        "Новый",
        "",
    ]


HEADERS = [
    "ID вакансии",
    "Дата сбора",
    "Дата публикации",
    "Компания",
    "Должность",
    "Зарплата",
    "Город",
    "Опыт",
    "Формат работы",
    "Категория",
    "Скоринг",
    "Почему",
    "Ключевые признаки управленки",
    "Положительные сигналы",
    "Стоп-факторы",
    "Описание вакансии",
    "Ссылка HH",
    "Адрес",
    "Статус",
    "Комментарий менеджера",
]


def google_client_from_env() -> gspread.Client:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if not raw:
        raise RuntimeError("Не задан GOOGLE_SERVICE_ACCOUNT_JSON")
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON содержит некорректный JSON") from exc

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    return gspread.authorize(creds)


def get_or_create_worksheet(spreadsheet: gspread.Spreadsheet, title: str) -> gspread.Worksheet:
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=2000, cols=len(HEADERS))
    values = ws.get_all_values()
    if not values:
        ws.append_row(HEADERS, value_input_option="RAW")
        ws.freeze(rows=1)
    elif values[0] != HEADERS:
        LOGGER.warning("На листе %s заголовки отличаются от ожидаемых. Новые строки будут добавлены по текущей схеме.", title)
    return ws


def existing_ids(ws: gspread.Worksheet) -> set[str]:
    values = ws.col_values(1)
    return {str(v).strip() for v in values[1:] if str(v).strip()}


def append_rows_batched(ws: gspread.Worksheet, rows: list[list[Any]], batch_size: int = 100) -> None:
    for i in range(0, len(rows), batch_size):
        ws.append_rows(rows[i : i + batch_size], value_input_option="RAW")


def run(config_path: Path) -> None:
    config = load_config(config_path)
    hh_token = os.environ.get("HH_ACCESS_TOKEN", "").strip()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("Не задан GOOGLE_SHEET_ID")

    hh = HHClient(
        user_agent=config.get("user_agent", "FinvedVacancyLeadParser/2.0 (contact: info@finved-finance.ru)"),
        access_token=hh_token,
        timeout=int(config.get("request_timeout_seconds", 60)),
        retries=int(config.get("request_retries", 4)),
        pause=float(config.get("request_pause_seconds", 0.35)),
    )

    gc = google_client_from_env()
    spreadsheet = gc.open_by_key(sheet_id)
    target_ws = get_or_create_worksheet(spreadsheet, config.get("target_sheet", "Целевые лиды"))
    review_ws = get_or_create_worksheet(spreadsheet, config.get("review_sheet", "Ручная проверка"))

    seen_ids = existing_ids(target_ws) | existing_ids(review_ws)
    LOGGER.info("В Google Sheets уже есть %s вакансий", len(seen_ids))

    collected: dict[str, dict[str, Any]] = {}
    for idx, query in enumerate(config["search_queries"], 1):
        LOGGER.info("[%s/%s] Поиск: %s", idx, len(config["search_queries"]), query)
        for item in hh.search_vacancies(query, config):
            vid = str(item.get("id", ""))
            if vid and vid not in seen_ids:
                collected[vid] = item

    LOGGER.info("Новых уникальных вакансий до анализа: %s", len(collected))
    collected_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    target_rows: list[list[Any]] = []
    review_rows: list[list[Any]] = []

    for idx, vacancy_id in enumerate(collected, 1):
        try:
            vacancy = hh.get_vacancy(vacancy_id)
            scoring = score_vacancy(vacancy, config)
            LOGGER.info("[%s/%s] %s — %s, %s", idx, len(collected), vacancy.get("name", ""), scoring["score"], scoring["category"])
            row = vacancy_to_row(vacancy, scoring, collected_at)
            if scoring["category"] == "Целевой лид":
                target_rows.append(row)
            elif scoring["category"] == "Ручная проверка" and config.get("write_review_sheet", True):
                review_rows.append(row)
        except Exception as exc:
            LOGGER.exception("Не удалось обработать вакансию %s: %s", vacancy_id, exc)

    if target_rows:
        append_rows_batched(target_ws, target_rows)
    if review_rows:
        append_rows_batched(review_ws, review_rows)

    LOGGER.info("Готово. Добавлено целевых: %s; на ручную проверку: %s", len(target_rows), len(review_rows))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Автоматический парсер HH → Google Sheets для ФИНВЕД")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config.json"))
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(message)s")
    try:
        run(args.config)
        return 0
    except Exception as exc:
        LOGGER.exception("Критическая ошибка: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

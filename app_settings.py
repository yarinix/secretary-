"""Схема редактируемых настроек Mini App.

ЧИСТЫЙ МОДУЛЬ: не импортирует `config`, aiogram, aiohttp и не читает env.
Значения окружения приходят сюда только как аргумент `cfg_like` в
`build_defaults()`, поэтому модуль полностью тестируется без переменных
окружения.

Договорённости:
  * В SQLite (`settings`) всё лежит TEXT. Для bool это "1"/"0" — так же
    читают команды /business в main.py, поэтому совместимость сохраняется.
  * Внутреннее состояние `state` (то, что уходит в /api/state и в main.py)
    использует "1"/"0" для bool, int для чисел, str и list для остального.
  * `encode()` — внутреннее значение -> канонический TEXT.
    `decode()` — TEXT из БД -> внутреннее значение с fallback на default.
  * `validate_patch()` — all-or-nothing: возвращает словарь ошибок; если он
    пуст, `canonical()` отдаёт готовые пары для атомарной записи в БД.
  * Ключ Gemini, BOT_TOKEN и прочие системные значения сюда НЕ входят и
    никогда не принимаются от клиента (неизвестный ключ = ошибка).
"""

from __future__ import annotations

import json
import re
from typing import Any

from helpers import clean_name

# Порядок = приоритет каскада. Только стандартные IANA-имена, которые есть
# в системной tzdata (на slim-образах дополнительно ставится пакет tzdata).
TIMEZONES = (
    "Europe/Kaliningrad",
    "Europe/Moscow",
    "Europe/Kiev",
    "Europe/Berlin",
    "Europe/London",
    "Asia/Yekaterinburg",
    "Asia/Omsk",
    "Asia/Novosibirsk",
    "Asia/Irkutsk",
    "Asia/Vladivostok",
    "Asia/Almaty",
    "Asia/Tashkent",
    "Asia/Tokyo",
    "America/New_York",
    "UTC",
)

MODEL_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Только для отображения в /api/state. Никогда не принимаются в /api/settings.
READONLY_KEYS = (
    "BOT_TOKEN",
    "OWNER_ID",
    "WEBHOOK_SECRET",
    "PORT",
    "DB_PATH",
    "BASE_URL",
    "APP_URL",
    "api_key_set",
)

# key -> {type, ...ограничения}. `default` НЕ здесь: он считается из env.
SPECS: dict[str, dict[str, Any]] = {
    "enabled":              {"type": "bool", "label": "Автоответы"},
    "inbox":                {"type": "bool", "label": "Копия входящих"},
    "notify":               {"type": "bool", "label": "Копия ответов"},
    "persona":              {"type": "text", "label": "Указания для бота"},
    "takeover_minutes":     {"type": "int", "min": 0, "max": 240,
                             "label": "Пауза после моего сообщения, мин"},
    "max_replies_per_hour": {"type": "int", "min": 0, "max": 100,
                             "label": "Лимит ответов в час"},
    # 40 — верхняя граница helpers.clean_name(); держим одно и то же число,
    # иначе длинное имя молча обрезалось бы вместо понятной ошибки валидации.
    "owner_name":           {"type": "name", "min": 1, "max": 40,
                             "label": "Как к вам обращаться"},
    "timezone":             {"type": "enum", "label": "Часовой пояс"},
    "ignore_ids":           {"type": "int_list", "max_items": 100,
                             "label": "Игнорируемые ID"},
    "models":               {"type": "str_list", "min_items": 1, "max_items": 8,
                             "label": "Каскад моделей"},
    "history_limit":        {"type": "int", "min": 0, "max": 50,
                             "label": "История, сообщений"},
    "persona_limit":        {"type": "int", "min": 100, "max": 2000,
                             "label": "Лимит указаний"},
}

EDITABLE_KEYS = tuple(SPECS)


# ---------------------------------------------------------------- defaults

def _get(cfg_like, key: str, default: Any = None) -> Any:
    if isinstance(cfg_like, dict):
        return cfg_like.get(key, default)
    return getattr(cfg_like, key, default)


def build_defaults(cfg_like) -> dict[str, Any]:
    """Слой дефолтов из окружения. `cfg_like` — модуль config или dict.

    Никакого чтения os.environ внутри: тесты передают простой объект.
    """
    models = _get(cfg_like, "MODELS") or ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
    ignore = _get(cfg_like, "IGNORE_IDS") or []
    try:
        ignore_list = sorted({int(x) for x in ignore if int(x) > 0})
    except (TypeError, ValueError):
        ignore_list = []

    def _int(key: str, fallback: int) -> int:
        try:
            return int(_get(cfg_like, key, fallback))
        except (TypeError, ValueError):
            return fallback

    tz = str(_get(cfg_like, "BOT_TIMEZONE", "Asia/Omsk") or "Asia/Omsk")
    return {
        "enabled": "1",
        "inbox": "1",
        "notify": "1",
        "persona": "",
        "takeover_minutes": _int("TAKEOVER_MINUTES", 60),
        "max_replies_per_hour": _int("MAX_REPLIES_PER_HOUR", 20),
        "owner_name": clean_name(_get(cfg_like, "OWNER_NAME", "владельца")) or "владельца",
        "timezone": tz if tz in TIMEZONES else "Asia/Omsk",
        "ignore_ids": ignore_list,
        "models": [str(m).strip() for m in models if str(m).strip()][:8]
                   or ["gemini-3.5-flash-lite"],
        "history_limit": _int("HISTORY_LIMIT", 12),
        "persona_limit": _int("PERSONA_LIMIT", 800),
    }


# ---------------------------------------------------------------- encode / decode

def encode(key: str, value: Any, defaults: dict) -> str:
    """Внутреннее значение -> канонический TEXT для таблицы settings."""
    if key not in SPECS:
        raise KeyError(f"неизвестный ключ настроек: {key}")
    kind = SPECS[key]["type"]

    if kind == "bool":
        if value in (True, 1, "1", "true", "True", "on"):
            return "1"
        if value in (False, 0, "0", "false", "False", "off", "", None):
            return "0"
        raise ValueError("ожидалось 1 / 0")
    if kind == "int":
        return str(int(value))
    if kind in ("text", "name", "enum"):
        return str(value if value is not None else "")
    if kind in ("int_list", "str_list"):
        return json.dumps(list(value), ensure_ascii=False, separators=(",", ":"))
    raise KeyError(f"неподдерживаемый тип: {kind}")


def decode(key: str, raw: str | None, defaults: dict) -> Any:
    """TEXT из БД -> внутреннее значение. Битые данные не роняют бота."""
    if key not in SPECS:
        raise KeyError(f"неизвестный ключ настроек: {key}")
    fallback = defaults[key]
    if raw is None:
        return fallback
    kind = SPECS[key]["type"]
    try:
        if kind == "bool":
            return "1" if str(raw).strip() in ("1", "true", "True", "on") else "0"
        if kind == "int":
            value = int(str(raw).strip())
            lo, hi = SPECS[key]["min"], SPECS[key]["max"]
            return value if lo <= value <= hi else fallback
        if kind in ("text", "name", "enum"):
            value = str(raw)
            if kind == "name":
                return clean_name(value)[: SPECS[key]["max"]] or fallback
            if kind == "enum":
                return value if value in TIMEZONES else fallback
            return _CONTROL_RE.sub("", value)
        if kind in ("int_list", "str_list"):
            value = json.loads(raw)
            if not isinstance(value, list):
                return fallback
            if kind == "int_list":
                return [int(x) for x in value if int(x) > 0][: SPECS[key]["max_items"]]
            return [str(x) for x in value if MODEL_RE.fullmatch(str(x))][: SPECS[key]["max_items"]] or fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback
    return fallback


def load_state(raw_rows: dict[str, str], defaults: dict) -> dict:
    """Полное состояние: env-дефолты, перекрытые строками из settings."""
    rows = raw_rows or {}
    return {key: decode(key, rows.get(key), defaults) for key in EDITABLE_KEYS}


# ---------------------------------------------------------------- validation

def _normalize(key: str, value: Any, defaults: dict, patch: dict) -> Any:
    """Приводит клиентское значение к внутреннему или бросает ValueError."""
    if key not in SPECS:
        raise ValueError("неизвестный параметр")
    spec = SPECS[key]
    kind = spec["type"]

    if kind == "bool":
        if value in (True, 1, "1", "true", "on"):
            return "1"
        if value in (False, 0, "0", "false", "off", "", None):
            return "0"
        raise ValueError("допустимо только вкл / выкл")

    if kind == "int":
        if isinstance(value, bool):
            raise ValueError("ожидалось целое число")
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError("ожидалось целое число")
        lo, hi = spec["min"], spec["max"]
        if not lo <= number <= hi:
            raise ValueError(f"допустимо {lo}–{hi}")
        return number

    if kind == "text":
        text = _CONTROL_RE.sub("", str(value if value is not None else ""))
        # Лимит берём из того же patch (если меняется), иначе из defaults —
        # иначе одновременно «увеличить лимит и текст» ложно отбросится.
        try:
            limit = int(patch.get("persona_limit", defaults["persona_limit"]))
        except (TypeError, ValueError):
            limit = int(defaults["persona_limit"])
        if len(text) > limit:
            raise ValueError(f"не длиннее {limit} символов")
        return text

    if kind == "name":
        # clean_name("") возвращает «Собеседник», поэтому пустое значение
        # отсекаем ДО очистки — иначе пустое имя молча подменится дефолтом.
        raw = str(value if value is not None else "")
        stripped = " ".join(_CONTROL_RE.sub(" ", raw).split())
        if not (spec["min"] <= len(stripped) <= spec["max"]):
            raise ValueError(f"от {spec['min']} до {spec['max']} символов")
        name = clean_name(raw)[: spec["max"]]
        if not name:
            raise ValueError(f"от {spec['min']} до {spec['max']} символов")
        return name

    if kind == "enum":
        tz = str(value or "")
        if tz not in TIMEZONES:
            raise ValueError("выберите часовой пояс из списка")
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(tz)
        except Exception:
            raise ValueError("часовой пояс недоступен в системе (нужен tzdata)")
        return tz

    if not isinstance(value, list):
        raise ValueError("ожидался список")

    if kind == "int_list":
        result: list[int] = []
        for item in value:
            try:
                number = int(item)
            except (TypeError, ValueError):
                raise ValueError("ID должен быть целым положительным числом")
            if number <= 0:
                raise ValueError("ID должен быть положительным")
            if number not in result:
                result.append(number)
        if len(result) > spec["max_items"]:
            raise ValueError(f"не больше {spec['max_items']} ID")
        return result

    # str_list — каскад моделей
    result_s: list[str] = []
    for item in value:
        model = str(item).strip()
        if not MODEL_RE.fullmatch(model):
            raise ValueError("недопустимое имя модели")
        if model in result_s:
            raise ValueError("модель повторяется")
        result_s.append(model)
    if not spec["min_items"] <= len(result_s) <= spec["max_items"]:
        raise ValueError(f"от {spec['min_items']} до {spec['max_items']} моделей")
    return result_s


def validate_patch(patch: dict, defaults: dict) -> dict[str, str]:
    """Проверяет весь patch целиком. Возвращает {key: сообщение}; {} = OK.

    All-or-nothing: при любой ошибке webapp не пишет ни одного значения.
    """
    if not isinstance(patch, dict):
        return {"settings": "ожидался объект"}
    errors: dict[str, str] = {}
    for key, value in patch.items():
        if key not in SPECS:
            errors[str(key)] = "неизвестный параметр"
            continue
        try:
            _normalize(key, value, defaults, patch)
        except ValueError as e:
            errors[key] = str(e)
    return errors


def canonical(patch: dict, defaults: dict) -> dict[str, str]:
    """Канонические TEXT-пары для БД. Вызывать ТОЛЬКО после успешной валидации."""
    return {
        key: encode(key, _normalize(key, value, defaults, patch), defaults)
        for key, value in patch.items()
    }


# ---------------------------------------------------------------- API helpers

def public_schema(defaults: dict) -> dict:
    """Метаданные для клиента: типы, границы, дефолты, список часовых поясов."""
    fields = {}
    for key, spec in SPECS.items():
        fields[key] = {
            "type": spec["type"],
            "label": spec["label"],
            "default": defaults[key],
        }
        for bound in ("min", "max", "min_items", "max_items"):
            if bound in spec:
                fields[key][bound] = spec[bound]
    return {
        "fields": fields,
        "timezones": list(TIMEZONES),
        "readonly": list(READONLY_KEYS),
    }


def cascade_kwargs(state: dict, api_key: str) -> dict:
    """Аргументы для GeminiCascade из живого состояния (объект без состояния)."""
    return {
        "api_key": (api_key or "").strip(),
        "models": list(state.get("models") or []) or list(build_defaults({})["models"]),
    }

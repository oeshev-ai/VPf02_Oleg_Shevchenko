"""Конфигурация приложения и настройка логирования.

Все параметры проекта собраны в этом модуле. Секреты читаются только из
переменных окружения (или из локального ``.env``), в коде они не хранятся.

Импорт модуля выполняет валидацию конфигурации. Если обязательные переменные
не заданы, поднимается :class:`ConfigError` с понятным сообщением — это
происходит ещё до запуска polling, поэтому бот не начнёт работу с неполным
конфигом.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Пути и значения по умолчанию
# ---------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
LOG_DIR = BASE_DIR / "logs"
LOG_FILE = LOG_DIR / "bot.log"

DEFAULT_AITUNNEL_BASE_URL = "https://api.aitunnel.ru/v1/"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TEMPERATURE = 0.7
DEFAULT_MAX_TOKENS = 512
DEFAULT_MAX_HISTORY_MESSAGES = 10
DEFAULT_LOG_LEVEL = "INFO"
DEFAULT_REQUEST_TIMEOUT_SECONDS = 60.0
DEFAULT_REQUEST_MAX_RETRIES = 2
DEFAULT_MESSAGE_CHUNK_SIZE = 4096
DEFAULT_SYSTEM_PROMPT = (
    "Ты полезный AI-ассистент в Telegram. Отвечай кратко и по делу на языке "
    "пользователя, если он не попросил иное."
)
DEFAULT_RATE_LIMIT_SECONDS = 2.0
DEFAULT_PERSIST_CONTEXT = True
DEFAULT_CONTEXT_FILE = "data/contexts.json"

# Ротация логов: 5 МБ на файл, 3 архивных копии.
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 3

LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Ограничения, защищающие от заведомо ошибочных значений в .env.
MIN_TEMPERATURE = 0.0
MAX_TEMPERATURE = 2.0
MIN_MAX_TOKENS = 1
MAX_MAX_TOKENS = 8192
# Минимум 2: иначе после ответа ассистента связный хвост некуда обрезать.
MIN_HISTORY_MESSAGES = 2
MAX_HISTORY_MESSAGES = 1000
MIN_TIMEOUT_SECONDS = 0.1
MAX_TIMEOUT_SECONDS = 600.0
MIN_RETRIES = 0
MAX_RETRIES = 10
MIN_MESSAGE_CHUNK_SIZE = 1
# Лимит одного сообщения Telegram.
MAX_MESSAGE_CHUNK_SIZE = 4096
LOCAL_HTTP_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
MIN_RATE_LIMIT_SECONDS = 0.0
MAX_RATE_LIMIT_SECONDS = 3600.0

ALLOWED_LOG_LEVELS = frozenset({"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"})
REQUIRED_SECRETS = ("BOT_TOKEN", "AITUNNEL_API_KEY")
_PLACEHOLDER_SECRETS = {
    "BOT_TOKEN": "your_telegram_bot_token",
    "AITUNNEL_API_KEY": "sk-aitunnel-xxx",
}
_TRUE_VALUES = frozenset({"1", "true", "yes", "on", "y"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", "n"})

load_dotenv(dotenv_path=ENV_FILE, override=False)


class ConfigError(RuntimeError):
    """Ошибка отсутствующей или некорректной конфигурации."""


# ---------------------------------------------------------------------------
# Разбор переменных окружения
# ---------------------------------------------------------------------------


def _validate_required_secrets() -> None:
    """Проверить все обязательные секреты и сообщить обо всех проблемах сразу."""

    problems: list[str] = []
    for name in REQUIRED_SECRETS:
        value = os.getenv(name, "").strip()
        if not value:
            problems.append(f"{name} не задана")
        elif value == _PLACEHOLDER_SECRETS.get(name):
            problems.append(f"{name} содержит заглушку из .env.example")

    if problems:
        details = "; ".join(problems)
        raise ConfigError(
            f"Некорректная конфигурация: {details}. "
            f"Создайте файл {ENV_FILE} на основе .env.example и заполните его."
        )


def _read_required(name: str) -> str:
    """Прочитать обязательную строковую переменную окружения."""

    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"Не задана обязательная переменная окружения {name}. "
            f"Создайте файл {ENV_FILE} на основе .env.example и заполните его."
        )
    return value


def _read_str(name: str, default: str) -> str:
    """Прочитать необязательную строковую переменную окружения."""

    value = os.getenv(name, "").strip()
    return value or default


def _read_int(
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int | None = None,
) -> int:
    """Прочитать целочисленную переменную и проверить её диапазон."""

    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ConfigError(
            f"{name} должно быть целым числом, получено: {raw_value!r}."
        ) from exc
    if value < minimum or (maximum is not None and value > maximum):
        bounds = (
            f"от {minimum} до {maximum}"
            if maximum is not None
            else f"не меньше {minimum}"
        )
        raise ConfigError(f"{name} должно быть {bounds}, получено: {value}.")
    return value


def _read_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    """Прочитать число с плавающей точкой и проверить его диапазон."""

    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return default
    try:
        value = float(raw_value.replace(",", "."))
    except ValueError as exc:
        raise ConfigError(
            f"{name} должно быть числом, получено: {raw_value!r}."
        ) from exc
    if not minimum <= value <= maximum:
        raise ConfigError(
            f"{name} должно быть от {minimum} до {maximum}, получено: {value}."
        )
    return value


def _read_base_url() -> str:
    """Прочитать и проверить base URL OpenAI-совместимого прокси."""

    value = _read_str("AITUNNEL_BASE_URL", DEFAULT_AITUNNEL_BASE_URL)
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(
            "AITUNNEL_BASE_URL должен быть полным HTTP(S)-адресом, "
            f"например {DEFAULT_AITUNNEL_BASE_URL}. Получено: {value!r}."
        )
    if parsed.scheme == "http" and (parsed.hostname or "").lower() not in LOCAL_HTTP_HOSTS:
        raise ConfigError(
            "AITUNNEL_BASE_URL должен использовать HTTPS "
            f"(http разрешён только для {', '.join(sorted(LOCAL_HTTP_HOSTS))}). "
            f"Получено: {value!r}."
        )
    # SDK ожидает base_url с завершающим слешем, иначе может потерять /v1.
    return value.rstrip("/") + "/"


def _read_log_level() -> str:
    """Прочитать и проверить уровень логирования."""

    value = _read_str("LOG_LEVEL", DEFAULT_LOG_LEVEL).upper()
    if value not in ALLOWED_LOG_LEVELS:
        allowed = ", ".join(sorted(ALLOWED_LOG_LEVELS))
        raise ConfigError(
            f"LOG_LEVEL должен быть одним из: {allowed}. Получено: {value!r}."
        )
    return value


def _read_bool(name: str, default: bool) -> bool:
    """Прочитать булеву переменную окружения."""

    raw_value = os.getenv(name, "").strip().lower()
    if not raw_value:
        return default
    if raw_value in _TRUE_VALUES:
        return True
    if raw_value in _FALSE_VALUES:
        return False
    raise ConfigError(
        f"{name} должно быть true/false (или 1/0), получено: {raw_value!r}."
    )


def _read_user_ids(name: str) -> frozenset[int]:
    """Прочитать список Telegram user_id через запятую. Пусто = без ограничений."""

    raw_value = os.getenv(name, "").strip()
    if not raw_value:
        return frozenset()

    user_ids: set[int] = set()
    for part in raw_value.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            user_ids.add(int(token))
        except ValueError as exc:
            raise ConfigError(
                f"{name} должно содержать целые Telegram id через запятую, "
                f"получено: {raw_value!r}."
            ) from exc
    return frozenset(user_ids)


def _read_context_file() -> Path:
    """Путь к JSON-файлу истории диалогов."""

    raw_value = _read_str("CONTEXT_FILE", DEFAULT_CONTEXT_FILE)
    path = Path(raw_value)
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


# ---------------------------------------------------------------------------
# Типизированные настройки
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Полный набор настроек приложения.

    Секретные поля исключены из ``repr``, чтобы токены случайно не попали
    в логи или в отладочный вывод.
    """

    bot_token: str = field(repr=False)
    aitunnel_api_key: str = field(repr=False)
    aitunnel_base_url: str
    default_model: str
    default_temperature: float
    default_max_tokens: int
    max_history_messages: int
    log_level: str
    log_dir: Path
    log_file: Path
    log_max_bytes: int
    log_backup_count: int
    request_timeout_seconds: float
    request_max_retries: int
    message_chunk_size: int
    system_prompt: str
    allowed_user_ids: frozenset[int]
    rate_limit_seconds: float
    persist_context: bool
    context_file: Path


def load_settings() -> Settings:
    """Собрать настройки из переменных окружения и провалидировать их."""

    _validate_required_secrets()

    return Settings(
        bot_token=_read_required("BOT_TOKEN"),
        aitunnel_api_key=_read_required("AITUNNEL_API_KEY"),
        aitunnel_base_url=_read_base_url(),
        default_model=_read_str("DEFAULT_MODEL", DEFAULT_MODEL),
        default_temperature=_read_float(
            "DEFAULT_TEMPERATURE",
            DEFAULT_TEMPERATURE,
            minimum=MIN_TEMPERATURE,
            maximum=MAX_TEMPERATURE,
        ),
        default_max_tokens=_read_int(
            "DEFAULT_MAX_TOKENS",
            DEFAULT_MAX_TOKENS,
            minimum=MIN_MAX_TOKENS,
            maximum=MAX_MAX_TOKENS,
        ),
        max_history_messages=_read_int(
            "MAX_HISTORY_MESSAGES",
            DEFAULT_MAX_HISTORY_MESSAGES,
            minimum=MIN_HISTORY_MESSAGES,
            maximum=MAX_HISTORY_MESSAGES,
        ),
        log_level=_read_log_level(),
        log_dir=LOG_DIR,
        log_file=LOG_FILE,
        log_max_bytes=LOG_MAX_BYTES,
        log_backup_count=LOG_BACKUP_COUNT,
        request_timeout_seconds=_read_float(
            "AITUNNEL_TIMEOUT_SECONDS",
            DEFAULT_REQUEST_TIMEOUT_SECONDS,
            minimum=MIN_TIMEOUT_SECONDS,
            maximum=MAX_TIMEOUT_SECONDS,
        ),
        request_max_retries=_read_int(
            "AITUNNEL_MAX_RETRIES",
            DEFAULT_REQUEST_MAX_RETRIES,
            minimum=MIN_RETRIES,
            maximum=MAX_RETRIES,
        ),
        message_chunk_size=_read_int(
            "MESSAGE_CHUNK_SIZE",
            DEFAULT_MESSAGE_CHUNK_SIZE,
            minimum=MIN_MESSAGE_CHUNK_SIZE,
            maximum=MAX_MESSAGE_CHUNK_SIZE,
        ),
        system_prompt=_read_str("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
        allowed_user_ids=_read_user_ids("ALLOWED_USER_IDS"),
        rate_limit_seconds=_read_float(
            "RATE_LIMIT_SECONDS",
            DEFAULT_RATE_LIMIT_SECONDS,
            minimum=MIN_RATE_LIMIT_SECONDS,
            maximum=MAX_RATE_LIMIT_SECONDS,
        ),
        persist_context=_read_bool("PERSIST_CONTEXT", DEFAULT_PERSIST_CONTEXT),
        context_file=_read_context_file(),
    )


settings = load_settings()

# Удобные константы для импорта: ``config.DEFAULT_MODEL`` и т. п.
BOT_TOKEN = settings.bot_token
AITUNNEL_API_KEY = settings.aitunnel_api_key
AITUNNEL_BASE_URL = settings.aitunnel_base_url
DEFAULT_MODEL = settings.default_model
DEFAULT_TEMPERATURE = settings.default_temperature
DEFAULT_MAX_TOKENS = settings.default_max_tokens
MAX_HISTORY_MESSAGES = settings.max_history_messages
LOG_LEVEL = settings.log_level
MESSAGE_CHUNK_SIZE = settings.message_chunk_size
AITUNNEL_TIMEOUT_SECONDS = settings.request_timeout_seconds
AITUNNEL_MAX_RETRIES = settings.request_max_retries
SYSTEM_PROMPT = settings.system_prompt
ALLOWED_USER_IDS = settings.allowed_user_ids
RATE_LIMIT_SECONDS = settings.rate_limit_seconds
PERSIST_CONTEXT = settings.persist_context
CONTEXT_FILE = settings.context_file


# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------


def configure_logging() -> None:
    """Настроить корневой логгер: консоль + ``logs/bot.log`` с ротацией.

    Файл создаётся автоматически. Повторный вызов заменяет обработчики,
    поэтому дублирования строк в логе не будет.
    """

    settings.log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    file_handler = RotatingFileHandler(
        filename=settings.log_file,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
        handler.close()
    root_logger.setLevel(settings.log_level)
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)
    # httpx пишет URL каждого запроса на INFO и забивает лог бота.
    for logger_name in ("httpx", "httpx2", "httpcore", "httpcore2"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)

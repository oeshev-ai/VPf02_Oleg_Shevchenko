"""Асинхронный клиент AITUNNEL (OpenAI-совместимый API).

Модуль инкапсулирует создание ``AsyncOpenAI``, один вызов Chat Completions,
извлечение ``request_id`` из заголовка ``X-Request-Id`` и логирование
параметров запроса и usage. API-ключ берётся только из конфигурации.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from openai import APIConnectionError, APIStatusError, AsyncOpenAI, OpenAIError

import config

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-Id"


class AIClientError(RuntimeError):
    """Понятная ошибка обращения к AITUNNEL.

    :ivar request_id: идентификатор запроса, если он известен.
    :ivar status_code: HTTP-статус ответа, если ошибка пришла от API.
    """

    def __init__(
        self,
        message: str,
        *,
        request_id: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.request_id = request_id
        self.status_code = status_code


# Один клиент на процесс: внутри AsyncOpenAI есть пул соединений.
client = AsyncOpenAI(
    api_key=config.AITUNNEL_API_KEY,
    base_url=config.AITUNNEL_BASE_URL,
    timeout=config.AITUNNEL_TIMEOUT_SECONDS,
    max_retries=config.AITUNNEL_MAX_RETRIES,
)


def _header_request_id(headers: Any) -> str | None:
    """Достать request id из заголовков, не падая на неожиданном типе."""

    if not headers:
        return None
    try:
        for header_name, value in headers.items():
            if str(header_name).lower() == REQUEST_ID_HEADER.lower() and value:
                return str(value)
    except (AttributeError, TypeError):
        return None
    return None


def _extract_request_id(source: Any) -> str | None:
    """Извлечь request id из ответа или исключения OpenAI SDK.

    SDK сохраняет заголовок ``X-Request-Id`` в приватном атрибуте
    ``_request_id`` объекта ответа, а у ошибок есть публичное свойство
    ``request_id``. Дополнительно проверяются заголовки HTTP-ответа.
    """

    if source is None:
        return None

    for attribute_name in ("_request_id", "request_id"):
        try:
            value = getattr(source, attribute_name)
        except (AttributeError, TypeError):
            continue
        if value:
            return str(value)

    response = getattr(source, "response", None)
    for container in (source, response):
        headers = getattr(container, "headers", None)
        request_id = _header_request_id(headers)
        if request_id:
            return request_id
    return None


def _usage_value(usage: Any, field_name: str) -> Any:
    """Прочитать поле usage, включая нестандартные поля AITUNNEL.

    ``cost_rub``, ``cost`` и ``balance`` не входят в стандартную схему
    OpenAI, поэтому pydantic складывает их в ``model_extra``.
    """

    if usage is None:
        return None

    if isinstance(usage, Mapping):
        return usage.get(field_name)

    value = getattr(usage, field_name, None)
    if value is not None:
        return value

    extra = getattr(usage, "model_extra", None)
    if isinstance(extra, Mapping):
        return extra.get(field_name)
    return None


def _log_usage(usage: Any, *, user_id: int | None, request_id: str | None) -> None:
    """Залогировать токены и рублёвую стоимость ответа."""

    if usage is None:
        logger.warning(
            "AITUNNEL usage отсутствует: user_id=%s request_id=%s",
            user_id,
            request_id,
        )
        return

    prompt_tokens = _usage_value(usage, "prompt_tokens")
    completion_tokens = _usage_value(usage, "completion_tokens")
    total_tokens = _usage_value(usage, "total_tokens")

    cost_rub = _usage_value(usage, "cost_rub")
    cost = _usage_value(usage, "cost")
    # В AITUNNEL cost_rub и cost — одно и то же число в рублях. Если прокси
    # прислал только cost, показываем его в поле cost_rub.
    effective_cost_rub = cost_rub if cost_rub is not None else cost
    balance = _usage_value(usage, "balance")

    logger.info(
        "AITUNNEL usage: user_id=%s prompt_tokens=%s completion_tokens=%s "
        "total_tokens=%s cost_rub=%s cost=%s balance=%s request_id=%s",
        user_id,
        prompt_tokens,
        completion_tokens,
        total_tokens,
        effective_cost_rub,
        cost,
        balance,
        request_id,
    )


async def get_completion(
    messages: Sequence[Mapping[str, str]],
    model: str = config.DEFAULT_MODEL,
    temperature: float = config.DEFAULT_TEMPERATURE,
    max_tokens: int = config.DEFAULT_MAX_TOKENS,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Запросить ответ у AITUNNEL через Chat Completions API.

    :param messages: история в формате OpenAI
        (``[{"role": "user", "content": "..."}]``).
    :param model: имя модели или слаг OpenRouter.
    :param temperature: температура генерации.
    :param max_tokens: максимальное число токенов ответа.
    :param user_id: Telegram id пользователя — только для логов.
    :returns: словарь с ключами ``content``, ``usage``, ``model`` и
        ``request_id``.
    :raises AIClientError: если API вернул ошибку или пустой ответ.
    """

    prepared_messages = list(messages)
    history_length = len(prepared_messages)

    logger.info(
        "AITUNNEL request: user_id=%s model=%s temperature=%s max_tokens=%s "
        "history_length=%s",
        user_id,
        model,
        temperature,
        max_tokens,
        history_length,
    )

    try:
        completion = await client.chat.completions.create(
            model=model,
            messages=prepared_messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except APIStatusError as exc:
        request_id = _extract_request_id(exc)
        logger.exception(
            "AITUNNEL API error: user_id=%s model=%s status_code=%s request_id=%s",
            user_id,
            model,
            exc.status_code,
            request_id,
        )
        raise AIClientError(
            f"AITUNNEL вернул ошибку HTTP {exc.status_code}.",
            request_id=request_id,
            status_code=exc.status_code,
        ) from exc
    except APIConnectionError as exc:
        request_id = _extract_request_id(exc)
        logger.exception(
            "AITUNNEL connection error: user_id=%s model=%s request_id=%s",
            user_id,
            model,
            request_id,
        )
        raise AIClientError(
            "Не удалось подключиться к AITUNNEL.",
            request_id=request_id,
        ) from exc
    except OpenAIError as exc:
        request_id = _extract_request_id(exc)
        logger.exception(
            "AITUNNEL OpenAI SDK error: user_id=%s model=%s request_id=%s",
            user_id,
            model,
            request_id,
        )
        raise AIClientError(
            "Ошибка при обращении к AITUNNEL.",
            request_id=request_id,
        ) from exc

    request_id = _extract_request_id(completion)
    usage = getattr(completion, "usage", None)
    _log_usage(usage, user_id=user_id, request_id=request_id)

    if not completion.choices:
        logger.error(
            "AITUNNEL вернул ответ без choices: user_id=%s model=%s request_id=%s",
            user_id,
            model,
            request_id,
        )
        raise AIClientError(
            "AITUNNEL вернул ответ без вариантов completion.",
            request_id=request_id,
        )

    content = completion.choices[0].message.content
    if not isinstance(content, str) or not content.strip():
        logger.error(
            "AITUNNEL вернул пустой content: user_id=%s model=%s request_id=%s",
            user_id,
            model,
            request_id,
        )
        raise AIClientError(
            "AITUNNEL вернул пустой текст ответа.",
            request_id=request_id,
        )

    response_model = getattr(completion, "model", None) or model

    return {
        "content": content,
        "usage": usage,
        "model": response_model,
        "request_id": request_id,
    }


async def close_client() -> None:
    """Закрыть HTTP-клиент и освободить соединения при остановке бота."""

    await client.close()

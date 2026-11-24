"""Telegram-бот с контекстным диалогом через AITUNNEL.

Запуск::

    python bot.py

Перед запуском заполните ``.env`` (см. ``.env.example``). История диалога
хранится в памяти и при ``PERSIST_CONTEXT=true`` сохраняется на диск.
"""

from __future__ import annotations

import asyncio
import logging
import time
import unicodedata
from contextlib import suppress

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatAction
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, Message

import config
from api_client import AIClientError, close_client, get_completion
from context_manager import ContextManager

logger = logging.getLogger(__name__)

dp = Dispatcher()
context_manager = ContextManager()
_user_locks: dict[int, asyncio.Lock] = {}
_last_request_at: dict[int, float] = {}

ERROR_MESSAGE = "Не удалось получить ответ, попробуйте позже"
UNSUPPORTED_MESSAGE = (
    "Я понимаю только текстовые сообщения. Напишите текст, и я отвечу."
)
HELP_MESSAGE = (
    "Доступные команды:\n"
    "/start — приветствие и краткая инструкция\n"
    "/reset — очистить контекст диалога\n"
    "/help — показать этот список\n\n"
    "Или просто напишите текстовое сообщение — я отвечу."
)
UNKNOWN_COMMAND_MESSAGE = (
    "Неизвестная команда. Напишите /help, чтобы увидеть список команд."
)
ACCESS_DENIED_MESSAGE = "Доступ к боту ограничен."
RATE_LIMIT_MESSAGE = "Слишком часто. Подождите пару секунд и напишите снова."
START_MESSAGE = (
    "Привет! Я AI-ассистент. Напишите мне сообщение, и я отвечу.\n\n"
    "Я помню контекст диалога. "
    "Чтобы начать разговор с чистого листа, отправьте /reset.\n"
    "Список команд: /help."
)
RESET_MESSAGE = "Контекст очищен. Можете начать новый диалог."
TYPING_REFRESH_SECONDS = 4.0

BOT_COMMANDS = [
    BotCommand(command="start", description="Начать работу с ботом"),
    BotCommand(command="reset", description="Очистить контекст диалога"),
    BotCommand(command="help", description="Список команд"),
]


def _message_text(message: Message) -> str:
    """Текст из обычного сообщения или подписи к медиа."""

    return (message.text or message.caption or "").strip()


def _log_incoming(message: Message, command: str | None = None) -> None:
    """Залогировать входящее сообщение без его содержимого."""

    user_id = message.from_user.id if message.from_user else None
    text = _message_text(message)
    detected_command = command
    if detected_command is None and text.startswith("/"):
        detected_command = text.split(maxsplit=1)[0]

    logger.info(
        "Incoming message: user_id=%s text_length=%s command=%s",
        user_id,
        len(text),
        detected_command,
    )


def _lock_for(user_id: int) -> asyncio.Lock:
    """Вернуть блокировку диалога одного пользователя.

    Создание происходит без ``await``, поэтому в одном event loop два
    обработчика не создадут разные замки для одного и того же id.
    """

    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


def _is_user_allowed(user_id: int) -> bool:
    """Проверить allowlist. Пустой список = доступ открыт всем."""

    if not config.ALLOWED_USER_IDS:
        return True
    return user_id in config.ALLOWED_USER_IDS


def _rate_limit_remaining(user_id: int) -> float:
    """Сколько секунд осталось ждать до следующего запроса (0 = можно)."""

    limit = config.RATE_LIMIT_SECONDS
    if limit <= 0:
        return 0.0
    last_at = _last_request_at.get(user_id)
    if last_at is None:
        return 0.0
    elapsed = time.monotonic() - last_at
    remaining = limit - elapsed
    return remaining if remaining > 0 else 0.0


def _mark_request(user_id: int) -> None:
    """Зафиксировать момент успешного старта запроса к модели."""

    if config.RATE_LIMIT_SECONDS > 0:
        _last_request_at[user_id] = time.monotonic()


def _messages_for_api(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Добавить system prompt к истории только для вызова API."""

    prompt = config.SYSTEM_PROMPT.strip()
    if not prompt:
        return history
    return [{"role": "system", "content": prompt}, *history]


def _is_emoji_only(text: str) -> bool:
    """Проверить, что в строке нет букв и цифр, только смайлы и символы."""

    has_symbol = False
    for char in text:
        if char.isspace():
            continue
        if unicodedata.category(char)[0] in {"L", "N"}:
            return False
        has_symbol = True
    return has_symbol


def _extract_command(text: str) -> str | None:
    """Вернуть имя команды (``/help``) без ``@bot``, если текст — команда."""

    if not text.startswith("/"):
        return None
    token = text.split(maxsplit=1)[0]
    return token.split("@", 1)[0].lower()


def _utf16_len(text: str) -> int:
    """Длина строки в UTF-16 code units (лимит Telegram Bot API)."""

    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def _find_utf16_split(text: str, limit: int) -> int:
    """Найти индекс разреза так, чтобы ``text[:index]`` уложился в ``limit`` UTF-16.

    Предпочитает перевод строки или пробел ближе к лимиту. Если разделителя
    нет, режет по границе символа, не превышая лимит.
    """

    if limit <= 0:
        return 0

    units = 0
    last_break = 0
    for index, char in enumerate(text):
        char_units = 2 if ord(char) > 0xFFFF else 1
        if units + char_units > limit:
            if last_break > 0:
                return last_break
            return index if index > 0 else 0
        units += char_units
        if char in {"\n", " "}:
            last_break = index + 1
    return len(text)


def _split_message(text: str, chunk_size: int) -> list[str]:
    """Разбить длинный ответ на части, помещающиеся в сообщение Telegram.

    Лимит считается в UTF-16 code units (как в Telegram Bot API), а не в
    Python ``len()``. По возможности разрез делается по переводу строки или
    пробелу. Пустые куски не попадают в результат.
    """

    chunks: list[str] = []
    remaining = text.strip()
    while remaining and _utf16_len(remaining) > chunk_size:
        split_at = _find_utf16_split(remaining, chunk_size)
        if split_at <= 0:
            # Символ сам по себе длиннее лимита — не должно случаться при
            # chunk_size >= 2; аварийно берём один символ, чтобы не зациклиться.
            split_at = 1
        piece = remaining[:split_at].strip()
        if piece:
            chunks.append(piece)
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


async def _answer(message: Message, text: str) -> None:
    """Отправить ответ, при необходимости несколькими сообщениями."""

    for chunk in _split_message(text, config.MESSAGE_CHUNK_SIZE):
        await message.answer(chunk)


async def _typing_keepalive(bot: Bot, chat_id: int, stop_event: asyncio.Event) -> None:
    """Поддерживать индикатор «печатает», пока идёт запрос к модели."""

    while not stop_event.is_set():
        with suppress(Exception):
            await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TYPING_REFRESH_SECONDS)
        except asyncio.TimeoutError:
            continue


async def _ensure_access(message: Message, user_id: int) -> bool:
    """Проверить allowlist; при отказе ответить и вернуть False."""

    if _is_user_allowed(user_id):
        return True
    logger.warning("Доступ запрещён allowlist: user_id=%s", user_id)
    await _answer(message, ACCESS_DENIED_MESSAGE)
    return False


@dp.message(CommandStart())
async def handle_start(message: Message) -> None:
    """Обработать /start и показать краткую инструкцию."""

    _log_incoming(message, command="/start")
    if message.from_user is None:
        return
    if not await _ensure_access(message, message.from_user.id):
        return
    await _answer(message, START_MESSAGE)


@dp.message(Command("reset"))
async def handle_reset(message: Message) -> None:
    """Очистить историю сообщений текущего пользователя."""

    _log_incoming(message, command="/reset")
    if message.from_user is None:
        logger.warning("Не удалось определить user_id при /reset")
        return
    if not await _ensure_access(message, message.from_user.id):
        return

    async with _lock_for(message.from_user.id):
        context_manager.clear_history(message.from_user.id)
        logger.info("Контекст очищен: user_id=%s", message.from_user.id)
        await _answer(message, RESET_MESSAGE)


@dp.message(Command("help"))
async def handle_help(message: Message) -> None:
    """Показать список доступных команд."""

    _log_incoming(message, command="/help")
    if message.from_user is None:
        return
    if not await _ensure_access(message, message.from_user.id):
        return
    await _answer(message, HELP_MESSAGE)


@dp.message(F.text | F.caption)
async def handle_text(message: Message) -> None:
    """Отправить текстовый запрос (или caption) в AITUNNEL с учётом истории."""

    _log_incoming(message)

    if message.from_user is None:
        logger.warning("Сообщение без from_user пропущено")
        return

    user_id = message.from_user.id
    if not await _ensure_access(message, user_id):
        return

    text = _message_text(message)

    if not text:
        await _answer(message, UNSUPPORTED_MESSAGE)
        return

    command = _extract_command(text)
    if command is not None:
        # Command-фильтры смотрят на message.text; команды в caption
        # попадают сюда и не должны уходить в модель.
        if command == "/start":
            await _answer(message, START_MESSAGE)
            return
        if command == "/reset":
            async with _lock_for(user_id):
                context_manager.clear_history(user_id)
                logger.info("Контекст очищен: user_id=%s", user_id)
                await _answer(message, RESET_MESSAGE)
            return
        if command == "/help":
            await _answer(message, HELP_MESSAGE)
            return
        logger.info(
            "Неизвестная команда отклонена: user_id=%s command=%s",
            user_id,
            command,
        )
        await _answer(message, UNKNOWN_COMMAND_MESSAGE)
        return

    if _is_emoji_only(text):
        logger.info(
            "Смайл/символы без текста: user_id=%s text_length=%s",
            user_id,
            len(text),
        )
        await _answer(message, UNSUPPORTED_MESSAGE)
        return

    remaining = _rate_limit_remaining(user_id)
    if remaining > 0:
        logger.info(
            "Rate limit: user_id=%s remaining=%.2fs",
            user_id,
            remaining,
        )
        await _answer(message, RATE_LIMIT_MESSAGE)
        return

    async with _lock_for(user_id):
        # Повторная проверка после ожидания лока: другой запрос мог пройти раньше.
        remaining = _rate_limit_remaining(user_id)
        if remaining > 0:
            await _answer(message, RATE_LIMIT_MESSAGE)
            return

        context_manager.add_message(user_id, "user", text)
        history = context_manager.get_history(user_id)
        api_messages = _messages_for_api(history)
        _mark_request(user_id)

        stop_typing = asyncio.Event()
        typing_task = asyncio.create_task(
            _typing_keepalive(message.bot, message.chat.id, stop_typing)
        )
        try:
            result = await get_completion(
                api_messages,
                model=config.DEFAULT_MODEL,
                temperature=config.DEFAULT_TEMPERATURE,
                max_tokens=config.DEFAULT_MAX_TOKENS,
                user_id=user_id,
            )
        except AIClientError as exc:
            context_manager.discard_last_message(user_id)
            logger.exception(
                "Не удалось получить ответ: user_id=%s request_id=%s",
                user_id,
                exc.request_id,
            )
            await _answer(message, ERROR_MESSAGE)
            return
        except Exception:
            context_manager.discard_last_message(user_id)
            logger.exception(
                "Непредвиденная ошибка при получении ответа: user_id=%s",
                user_id,
            )
            await _answer(message, ERROR_MESSAGE)
            return
        finally:
            stop_typing.set()
            typing_task.cancel()
            with suppress(asyncio.CancelledError):
                await typing_task

        content = result["content"]
        try:
            await _answer(message, content)
        except Exception:
            logger.exception(
                "Не удалось отправить ответ в Telegram: user_id=%s",
                user_id,
            )
            with suppress(Exception):
                await _answer(message, ERROR_MESSAGE)
            return

        context_manager.add_message(user_id, "assistant", content)


@dp.message(F.sticker)
async def handle_sticker(message: Message) -> None:
    """Пропустить стикер: это не запрос, отвечать на него не нужно."""

    _log_incoming(message)
    if message.from_user and not _is_user_allowed(message.from_user.id):
        return
    logger.info(
        "Стикер пропущен без ответа: user_id=%s",
        message.from_user.id if message.from_user else None,
    )


@dp.message()
async def handle_unsupported(message: Message) -> None:
    """Ответить на фото без подписи, голос и прочие сообщения без текста."""

    _log_incoming(message)
    if message.from_user is None:
        return
    if not await _ensure_access(message, message.from_user.id):
        return
    await _answer(message, UNSUPPORTED_MESSAGE)


async def main() -> None:
    """Настроить логирование и запустить long polling."""

    config.configure_logging()
    logger.info(
        "Запуск бота: model=%s temperature=%s max_tokens=%s "
        "max_history_messages=%s base_url=%s persist=%s rate_limit=%s "
        "allowlist=%s system_prompt=%s",
        config.DEFAULT_MODEL,
        config.DEFAULT_TEMPERATURE,
        config.DEFAULT_MAX_TOKENS,
        config.MAX_HISTORY_MESSAGES,
        config.AITUNNEL_BASE_URL,
        config.PERSIST_CONTEXT,
        config.RATE_LIMIT_SECONDS,
        len(config.ALLOWED_USER_IDS) or "open",
        "yes" if config.SYSTEM_PROMPT.strip() else "no",
    )

    bot = Bot(token=config.BOT_TOKEN)
    try:
        bot_info = await bot.get_me()
        logger.info(
            "Бот авторизован: id=%s username=%s",
            bot_info.id,
            bot_info.username,
        )
        await bot.set_my_commands(BOT_COMMANDS)
        logger.info(
            "Меню команд установлено: %s",
            ", ".join(f"/{item.command}" for item in BOT_COMMANDS),
        )
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except Exception:
        logger.exception("Критическая ошибка при работе бота")
        raise
    finally:
        with suppress(Exception):
            await close_client()
        with suppress(Exception):
            await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Штатная остановка по Ctrl+C: polling уже завершил работу.
        pass

"""Хранение истории диалога в памяти с опциональной записью на диск.

Ключ словаря — Telegram ``user_id``, значение — список сообщений формата
``{"role": "user" | "assistant" | "system", "content": "..."}``.

При ``PERSIST_CONTEXT=true`` история сохраняется в JSON и переживает
перезапуск бота. Параллельные апдейты одного пользователя сериализует
``_user_locks`` в ``bot.py``.
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Literal

import config

logger = logging.getLogger(__name__)

Role = Literal["user", "assistant", "system"]
ALLOWED_ROLES: frozenset[str] = frozenset({"user", "assistant", "system"})


class ContextManager:
    """Хранит и обрезает историю сообщений каждого пользователя."""

    def __init__(
        self,
        max_history_messages: int = config.MAX_HISTORY_MESSAGES,
        *,
        persist: bool = config.PERSIST_CONTEXT,
        storage_path: Path | None = None,
    ) -> None:
        """Создать хранилище и при необходимости загрузить историю с диска.

        :param max_history_messages: сколько последних сообщений хранить.
        :param persist: сохранять ли историю в JSON после изменений.
        :param storage_path: путь к файлу; по умолчанию ``config.CONTEXT_FILE``.
        :raises ValueError: если лимит меньше единицы.
        """

        if max_history_messages < 1:
            raise ValueError("max_history_messages должен быть не меньше 1")
        self._max_history_messages = max_history_messages
        self._persist = persist
        self._storage_path = storage_path or config.CONTEXT_FILE
        self._histories: dict[int, list[dict[str, str]]] = {}
        if self._persist:
            self._load()

    @property
    def max_history_messages(self) -> int:
        """Текущий лимит хранимых сообщений."""

        return self._max_history_messages

    def add_message(self, user_id: int, role: Role, content: str) -> None:
        """Добавить сообщение и оставить только последние N сообщений.

        :param user_id: идентификатор пользователя Telegram.
        :param role: роль автора сообщения.
        :param content: текст сообщения.
        :raises ValueError: если передана неизвестная роль.
        """

        if role not in ALLOWED_ROLES:
            raise ValueError(f"Недопустимая роль сообщения: {role!r}")

        history = self._histories.setdefault(user_id, [])
        history.append({"role": role, "content": content})
        history[:] = self._coherent_tail(history)
        self._save()

    def discard_last_message(self, user_id: int) -> None:
        """Убрать последнее сообщение, если запрос к модели не удался."""

        history = self._histories.get(user_id)
        if not history:
            return
        history.pop()
        if not history:
            self._histories.pop(user_id, None)
        self._save()

    def _coherent_tail(self, history: list[dict[str, str]]) -> list[dict[str, str]]:
        """Оставить хвост истории, который начинается с реплики пользователя.

        Простая обрезка по числу сообщений может выкинуть вопрос и оставить
        ответ модели первым. Такой хвост в API не отправляется.

        Если в окне из N последних сообщений не осталось ``user`` (например,
        при N=1 после ответа ассистента), расширяем окно назад до последней
        реплики пользователя, чтобы не потерять только что сохранённый ответ.
        """

        if not history:
            return []

        start = max(0, len(history) - self._max_history_messages)
        while start < len(history) and history[start]["role"] != "user":
            start += 1

        if start >= len(history):
            for index in range(len(history) - 1, -1, -1):
                if history[index]["role"] == "user":
                    return history[index:]
            return []

        return history[start:]

    def get_history(self, user_id: int) -> list[dict[str, str]]:
        """Вернуть копию истории пользователя (последние N сообщений)."""

        history = self._histories.get(user_id, [])
        return [message.copy() for message in self._coherent_tail(history)]

    def clear_history(self, user_id: int) -> None:
        """Полностью удалить историю пользователя."""

        self._histories.pop(user_id, None)
        self._save()

    def _load(self) -> None:
        """Загрузить истории из JSON, игнорируя битый или пустой файл."""

        path = self._storage_path
        if not path.exists():
            logger.info("Файл истории отсутствует, старт с пустым контекстом: %s", path)
            return

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("Не удалось прочитать историю из %s", path)
            return

        if not isinstance(raw, dict):
            logger.warning("Некорректный формат истории в %s, игнорирую", path)
            return

        loaded = 0
        for key, messages in raw.items():
            try:
                user_id = int(key)
            except (TypeError, ValueError):
                continue
            if not isinstance(messages, list):
                continue
            cleaned: list[dict[str, str]] = []
            for item in messages:
                if not isinstance(item, dict):
                    continue
                role = item.get("role")
                content = item.get("content")
                if role not in ALLOWED_ROLES or not isinstance(content, str):
                    continue
                cleaned.append({"role": role, "content": content})
            if cleaned:
                self._histories[user_id] = self._coherent_tail(cleaned)
                loaded += 1

        logger.info(
            "История загружена: users=%s path=%s",
            loaded,
            path,
        )

    def _save(self) -> None:
        """Атомарно сохранить текущие истории в JSON."""

        if not self._persist:
            return

        path = self._storage_path
        payload = {
            str(user_id): messages
            for user_id, messages in self._histories.items()
            if messages
        }

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                delete=False,
                suffix=".tmp",
            ) as tmp_file:
                json.dump(payload, tmp_file, ensure_ascii=False, indent=2)
                tmp_file.write("\n")
                tmp_name = tmp_file.name
            Path(tmp_name).replace(path)
        except OSError:
            logger.exception("Не удалось сохранить историю в %s", path)

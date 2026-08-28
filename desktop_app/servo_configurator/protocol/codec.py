"""Кодирование и разбор кадров JSON Lines.

Раздел 1 `docs/PROTOCOL.md`. Модуль не знает ни о Serial, ни о Qt: на вход байты,
на выход типы из :mod:`.messages`. Благодаря этому разбор протокола покрывается
обычными тестами без оборудования и без событийного цикла.
"""

from __future__ import annotations

import json
from typing import Any

from .errors import ProtocolError
from .messages import (
    MAX_LINE_BYTES,
    Notification,
    Request,
    Response,
)

_TERMINATOR = b"\n"


def encode_request(request: Request) -> bytes:
    """Сериализует команду в кадр, готовый к записи в порт.

    ``separators`` без пробелов и ``ensure_ascii`` экономят место в кадре:
    ограничение в 512 байт на строку нужно соблюдать вместе с полезной нагрузкой
    ``set_config``, которая передаёт всю конфигурацию целиком.
    """
    payload = json.dumps(
        request.to_dict(),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")

    frame = payload + _TERMINATOR
    if len(frame) > MAX_LINE_BYTES:
        raise ProtocolError(
            f"кадр {len(frame)} Б превышает лимит {MAX_LINE_BYTES} Б: {request.cmd}"
        )
    return frame


def decode_message(line: str) -> Response | Notification:
    """Разбирает одну строку в ответ или событие.

    :raises ProtocolError: строка не является JSON-объектом либо не содержит
        признаков ни ответа, ни события.
    """
    try:
        message = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ProtocolError(f"строка не разобрана как JSON: {exc}") from exc

    if not isinstance(message, dict):
        raise ProtocolError(f"ожидался объект, получен {type(message).__name__}")

    if "evt" in message:
        return _decode_notification(message)
    if "id" in message and "ok" in message:
        return _decode_response(message)

    raise ProtocolError(f"неизвестный тип сообщения: ключи {sorted(message)}")


def _decode_response(message: dict[str, Any]) -> Response:
    message_id = message.get("id")
    if not isinstance(message_id, int) or isinstance(message_id, bool):
        raise ProtocolError(f"некорректный id ответа: {message_id!r}")

    ok = message.get("ok")
    if not isinstance(ok, bool):
        raise ProtocolError(f"поле ok должно быть булевым, получено {ok!r}")

    data = message.get("data")
    err = message.get("err")
    msg = message.get("msg")

    if not ok and not isinstance(err, str):
        raise ProtocolError("отказ без кода ошибки в поле err")

    return Response(
        id=message_id,
        ok=ok,
        data=data if isinstance(data, dict) else {},
        err=err if isinstance(err, str) else None,
        msg=msg if isinstance(msg, str) else "",
    )


def _decode_notification(message: dict[str, Any]) -> Notification:
    evt = message.get("evt")
    if not isinstance(evt, str) or not evt:
        raise ProtocolError(f"некорректное имя события: {evt!r}")

    data = message.get("data")
    return Notification(evt=evt, data=data if isinstance(data, dict) else {})


class LineAssembler:
    """Собирает кадры из потока байтов, приходящего кусками произвольной длины.

    Serial отдаёт данные без учёта границ сообщений: одно чтение может вернуть
    половину кадра, полтора кадра или три кадра сразу. Сборщик накапливает байты
    и отдаёт только завершённые строки.

    Устойчивость к мусору (раздел 1 протокола):

    * строка длиннее :data:`MAX_LINE_BYTES` отбрасывается целиком, а вместе с ней
      и остаток до следующего разделителя — он заведомо является хвостом
      испорченного кадра;
    * непригодные к декодированию байты отбрасываются, а не роняют чтение;
    * пустые строки (перевод строки в начале потока, ``\\r\\n``) игнорируются.

    Счётчик :attr:`dropped` позволяет показать в логе, что линия шумит.
    """

    def __init__(self, max_line_bytes: int = MAX_LINE_BYTES) -> None:
        self._max_line_bytes = max_line_bytes
        self._buffer = bytearray()
        self._skip_next = False
        self.dropped = 0
        """Число отброшенных кадров с момента создания сборщика."""

    def reset(self) -> None:
        """Сбрасывает состояние: вызывается при переподключении к порту."""
        self._buffer.clear()
        self._skip_next = False

    def feed(self, chunk: bytes) -> list[str]:
        """Добавляет порцию байтов и возвращает завершённые строки."""
        self._buffer.extend(chunk)
        lines: list[str] = []

        while (index := self._buffer.find(_TERMINATOR)) != -1:
            raw = bytes(self._buffer[:index])
            del self._buffer[: index + 1]

            if self._skip_next:
                # Хвост кадра, начало которого уже отброшено по переполнению.
                self._skip_next = False
                self.dropped += 1
                continue

            if len(raw) > self._max_line_bytes:
                self.dropped += 1
                continue

            try:
                line = raw.decode("utf-8").strip("\r")
            except UnicodeDecodeError:
                self.dropped += 1
                continue

            if line:
                lines.append(line)

        if len(self._buffer) > self._max_line_bytes:
            # Разделитель так и не встретился — накопленное является мусором.
            self._buffer.clear()
            self._skip_next = True
            self.dropped += 1

        return lines

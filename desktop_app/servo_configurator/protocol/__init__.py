"""Протокол обмена ПК ↔ ESP32 (JSON Lines).

Слой не зависит ни от Serial, ни от UI: он описывает формат сообщений, схему
конфигурации и правила их разбора. Спецификация — `docs/PROTOCOL.md`.
"""

from .codec import LineAssembler, decode_message, encode_request
from .errors import (
    CommandError,
    CommandTimeout,
    DeviceError,
    LinkError,
    ProtocolError,
    TransportError,
    describe,
)
from .messages import (
    IDEMPOTENT_COMMANDS,
    MAX_LINE_BYTES,
    PROTOCOL_VERSION,
    Command,
    DeviceState,
    Direction,
    Event,
    HomingResult,
    Notification,
    Request,
    Response,
    Telemetry,
)
from .schema import (
    CONFIG_VERSION,
    PARAMS,
    PARAMS_BY_PATH,
    EnumParam,
    IntParam,
    default_config,
    get_value,
    merge_config,
    set_value,
    validate_config,
)

__all__ = [
    "CONFIG_VERSION",
    "Command",
    "CommandError",
    "CommandTimeout",
    "DeviceError",
    "DeviceState",
    "Direction",
    "EnumParam",
    "Event",
    "HomingResult",
    "IDEMPOTENT_COMMANDS",
    "IntParam",
    "LineAssembler",
    "LinkError",
    "MAX_LINE_BYTES",
    "Notification",
    "PARAMS",
    "PARAMS_BY_PATH",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "Request",
    "Response",
    "Telemetry",
    "TransportError",
    "decode_message",
    "default_config",
    "describe",
    "encode_request",
    "get_value",
    "merge_config",
    "set_value",
    "validate_config",
]

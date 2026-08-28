"""Сервисный слой приложения: связывает UI с устройством."""

from .service import DEFAULT_TELEMETRY_PERIOD_MS, ServoService

__all__ = ["DEFAULT_TELEMETRY_PERIOD_MS", "ServoService"]

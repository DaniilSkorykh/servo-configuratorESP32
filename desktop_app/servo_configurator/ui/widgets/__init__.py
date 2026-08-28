"""Виджеты интерфейса, по одному на функциональный блок задания."""

from .charts_panel import ChartsPanel
from .config_panels import HomingPanel, OperatingPanel
from .connection_panel import ConnectionPanel
from .manual_panel import ManualPanel
from .param_form import ParamForm
from .telemetry_panel import TelemetryPanel

__all__ = [
    "ChartsPanel",
    "ConnectionPanel",
    "HomingPanel",
    "ManualPanel",
    "OperatingPanel",
    "ParamForm",
    "TelemetryPanel",
]

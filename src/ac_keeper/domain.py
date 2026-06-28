from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


@dataclass(frozen=True)
class TemperatureReading:
    sensor_name: str
    temperature_c: float
    humidity_pct: float | None = None
    observed_at: datetime = field(default_factory=utc_now)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AcStatus:
    power: bool | None
    mode: str | None
    target_temperature_c: float | None
    current_temperature_c: float | None
    observed_at: datetime = field(default_factory=utc_now)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ControlDecision:
    target_c: float
    measured_c: float | None
    action: str
    reason: str
    requested_power: bool | None = None
    requested_mode: str | None = None
    requested_setpoint_c: float | None = None
    created_at: datetime = field(default_factory=utc_now)

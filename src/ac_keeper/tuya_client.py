from __future__ import annotations

import random
from abc import ABC, abstractmethod
from typing import Any

from .config import AcDeviceConfig
from .domain import AcStatus


class AcClient(ABC):
    @abstractmethod
    def status(self) -> AcStatus:
        raise NotImplementedError

    @abstractmethod
    def apply(self, power: bool | None, mode: str | None, setpoint_c: float | None) -> None:
        raise NotImplementedError


class SimulatedAcClient(AcClient):
    def __init__(self, config: AcDeviceConfig):
        self.config = config
        self._power = False
        self._mode = config.modes.cool
        self._target = 21.5
        self._current = 22.0

    def status(self) -> AcStatus:
        if self._power and self._mode == self.config.modes.cool:
            self._current -= random.uniform(0.01, 0.05)
        elif self._power and self._mode == self.config.modes.heat:
            self._current += random.uniform(0.01, 0.05)
        else:
            self._current += random.uniform(-0.02, 0.03)
        return AcStatus(
            power=self._power,
            mode=self._mode,
            target_temperature_c=round(self._target, 2),
            current_temperature_c=round(self._current, 2),
            raw={"provider": "simulated"},
        )

    def apply(self, power: bool | None, mode: str | None, setpoint_c: float | None) -> None:
        if power is not None:
            self._power = power
        if mode is not None:
            self._mode = mode
        if setpoint_c is not None:
            self._target = setpoint_c


class TinyTuyaAcClient(AcClient):
    def __init__(self, config: AcDeviceConfig):
        if not config.device_id or not config.address or not config.local_key:
            raise ValueError("AC config is missing Tuya device_id, address, or local_key")
        self.config = config
        self._device = _build_tuya_device(config.device_id, config.address, config.local_key, config.version)

    def status(self) -> AcStatus:
        status = self._device.status()
        dps = status.get("dps", status)
        return AcStatus(
            power=_to_bool(dps.get(self.config.dps.power)),
            mode=_to_str_or_none(dps.get(self.config.dps.mode)),
            target_temperature_c=_from_device_temperature(
                dps.get(self.config.dps.target_temperature), self.config.temperature_scale
            ),
            current_temperature_c=_from_device_temperature(
                dps.get(self.config.dps.current_temperature), self.config.temperature_scale
            )
            if self.config.dps.current_temperature
            else None,
            raw={"provider": "tinytuya", "dps": dps},
        )

    def apply(self, power: bool | None, mode: str | None, setpoint_c: float | None) -> None:
        if power is not None:
            self._set_dp(self.config.dps.power, bool(power))
        if mode is not None:
            self._set_dp(self.config.dps.mode, mode)
        if setpoint_c is not None:
            self._set_dp(self.config.dps.target_temperature, _to_device_temperature(setpoint_c, self.config.temperature_scale))

    def _set_dp(self, dp: str, value: Any) -> None:
        dp_value = int(dp) if str(dp).isdigit() else dp
        result = self._device.set_value(dp_value, value)
        if isinstance(result, dict) and result.get("Error"):
            raise RuntimeError(f"Tuya set_value failed for DP {dp}: {result}")


def build_ac_client(config: AcDeviceConfig) -> AcClient:
    provider = config.provider.lower()
    if provider == "simulated":
        return SimulatedAcClient(config)
    if provider == "tinytuya":
        return TinyTuyaAcClient(config)
    raise ValueError(f"Unknown AC provider {config.provider!r}")


def _build_tuya_device(device_id: str, address: str, local_key: str, version: str) -> Any:
    try:
        import tinytuya
    except ImportError as exc:
        raise RuntimeError("Tuya support requires tinytuya. Install with `pip install -e .`.") from exc

    device = tinytuya.Device(device_id, address, local_key)
    device.set_version(float(version))
    return device


def _from_device_temperature(raw_value: Any, scale: float) -> float | None:
    if raw_value is None:
        return None
    return round(float(raw_value) * scale, 2)


def _to_device_temperature(value_c: float, scale: float) -> int | float:
    if scale == 0:
        raise ValueError("temperature_scale cannot be 0")
    raw = value_c / scale
    rounded = round(raw)
    return int(rounded) if abs(raw - rounded) < 0.001 else round(raw, 2)


def _to_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.lower() in {"1", "true", "on", "yes"}
    return None


def _to_str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)

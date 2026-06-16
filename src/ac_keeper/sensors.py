from __future__ import annotations

import json
import math
import random
import time
import urllib.request
from abc import ABC, abstractmethod
from typing import Any

from .config import SensorConfig
from .domain import TemperatureReading


class SensorReader(ABC):
    @abstractmethod
    def read(self) -> TemperatureReading:
        raise NotImplementedError


class SimulatedSensorReader(SensorReader):
    def __init__(self, config: SensorConfig):
        self.config = config
        self._phase = random.random() * math.pi

    def read(self) -> TemperatureReading:
        slow_wave = math.sin((time.time() / 180.0) + self._phase) * 0.5
        noise = random.uniform(-0.08, 0.08)
        value = 22.0 + slow_wave + noise + self.config.offset_c
        return TemperatureReading(
            sensor_name=self.config.name,
            temperature_c=round(value, 2),
            raw={"provider": "simulated"},
        )


class HttpJsonSensorReader(SensorReader):
    def __init__(self, config: SensorConfig):
        if not config.url:
            raise ValueError(f"Sensor {config.name} is missing url")
        self.config = config

    def read(self) -> TemperatureReading:
        request = urllib.request.Request(self.config.url, headers=self.config.headers)
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
        raw_value = _resolve_path(payload, self.config.value_path)
        temperature = _to_temperature(raw_value, self.config.scale, self.config.offset_c)
        return TemperatureReading(
            sensor_name=self.config.name,
            temperature_c=temperature,
            raw={"provider": "http_json", "value": raw_value},
        )


class TinyTuyaSensorReader(SensorReader):
    def __init__(self, config: SensorConfig):
        if not config.device_id or not config.address or not config.local_key:
            raise ValueError(f"Sensor {config.name} is missing Tuya device_id, address, or local_key")
        self.config = config
        self._device = _build_tuya_device(config.device_id, config.address, config.local_key, config.version)

    def read(self) -> TemperatureReading:
        status = self._device.status()
        dps = status.get("dps", status)
        raw_value = dps.get(str(self.config.temp_dp))
        if raw_value is None:
            raise KeyError(f"Tuya sensor {self.config.name} has no DP {self.config.temp_dp}; got {sorted(dps)}")
        return TemperatureReading(
            sensor_name=self.config.name,
            temperature_c=_to_temperature(raw_value, self.config.scale, self.config.offset_c),
            raw={"provider": "tinytuya", "dps": dps},
        )


def build_sensor_readers(configs: list[SensorConfig]) -> list[SensorReader]:
    readers: list[SensorReader] = []
    for config in configs:
        provider = config.provider.lower()
        if provider == "simulated":
            readers.append(SimulatedSensorReader(config))
        elif provider == "http_json":
            readers.append(HttpJsonSensorReader(config))
        elif provider == "tinytuya":
            readers.append(TinyTuyaSensorReader(config))
        else:
            raise ValueError(f"Unknown sensor provider {config.provider!r} for {config.name}")
    return readers


def _build_tuya_device(device_id: str, address: str, local_key: str, version: str) -> Any:
    try:
        import tinytuya
    except ImportError as exc:
        raise RuntimeError("Tuya support requires tinytuya. Install with `pip install -e .`.") from exc

    device = tinytuya.Device(device_id, address, local_key)
    device.set_version(float(version))
    return device


def _resolve_path(payload: Any, path: str) -> Any:
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current[part]
        elif isinstance(current, list):
            current = current[int(part)]
        else:
            raise KeyError(f"Cannot resolve {path!r}; {part!r} reached {type(current).__name__}")
    return current


def _to_temperature(raw_value: Any, scale: float, offset_c: float) -> float:
    return round(float(raw_value) * scale + offset_c, 2)

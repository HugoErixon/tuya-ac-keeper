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
            humidity_pct=round(45.0 + math.sin(time.time() / 240.0 + self._phase) * 3.0, 1),
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
        humidity = _read_humidity_path(payload, self.config)
        return TemperatureReading(
            sensor_name=self.config.name,
            temperature_c=temperature,
            humidity_pct=humidity,
            raw={"provider": "http_json", "value": raw_value, "humidity": humidity},
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
        humidity = _read_humidity_dp(dps, self.config)
        return TemperatureReading(
            sensor_name=self.config.name,
            temperature_c=_to_temperature(raw_value, self.config.scale, self.config.offset_c),
            humidity_pct=humidity,
            raw={"provider": "tinytuya", "dps": dps},
        )


class _MqttSensorHub:
    """En MQTT-anslutning som prenumererar på en FAST lista topics (känd före
    anslutning) och cachar senaste payload per topic. Topics prenumereras i
    on_connect — samma beprövade mönster som fungerar i andra lyssnare. MQTT är
    push-baserat men sensor-läsningen är pull-baserad; cachen överbryggar."""

    def __init__(self, host: str, port: int, topics: list[str]):
        import paho.mqtt.client as mqtt

        self._latest: dict[str, Any] = {}
        self._topics = list(topics)
        try:
            self._client = mqtt.Client(mqtt.CALLBACK_API_VERSION.VERSION2)
        except (AttributeError, TypeError):
            self._client = mqtt.Client()
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.connect(host, port, 60)
        self._client.loop_start()

    def _on_connect(self, client, userdata, flags, *args) -> None:
        for topic in self._topics:
            client.subscribe(topic)

    def _on_message(self, client, userdata, msg) -> None:
        try:
            self._latest[msg.topic] = json.loads(msg.payload.decode("utf-8"))
        except Exception:
            pass

    def latest(self, topic: str) -> Any:
        return self._latest.get(topic)


class MqttSensorReader(SensorReader):
    """Läser temperatur från en MQTT-topic (t.ex. Zigbee2MQTT). Payloaden tolkas som
    JSON och temperaturen plockas via value_path (default 'temperature')."""

    def __init__(self, config: SensorConfig, hub: _MqttSensorHub):
        self.config = config
        self._hub = hub

    def read(self) -> TemperatureReading:
        payload = self._hub.latest(self.config.topic)
        if payload is None:
            raise RuntimeError(f"No MQTT data yet for topic {self.config.topic}")
        raw_value = _resolve_path(payload, self.config.value_path)
        humidity = _read_humidity_path(payload, self.config)
        return TemperatureReading(
            sensor_name=self.config.name,
            temperature_c=_to_temperature(raw_value, self.config.scale, self.config.offset_c),
            humidity_pct=humidity,
            raw={"provider": "mqtt", "topic": self.config.topic, "value": raw_value, "humidity": humidity},
        )


def build_sensor_readers(configs: list[SensorConfig]) -> list[SensorReader]:
    # Skapa EN gemensam MQTT-hub med alla mqtt-topics kända i förväg (race-fritt).
    mqtt_configs = [c for c in configs if c.provider.lower() == "mqtt"]
    mqtt_hub: _MqttSensorHub | None = None
    if mqtt_configs:
        for c in mqtt_configs:
            if not c.topic:
                raise ValueError(f"Sensor {c.name} is missing topic")
        mqtt_hub = _MqttSensorHub(
            mqtt_configs[0].mqtt_host,
            mqtt_configs[0].mqtt_port,
            [c.topic for c in mqtt_configs],
        )

    readers: list[SensorReader] = []
    for config in configs:
        provider = config.provider.lower()
        if provider == "simulated":
            readers.append(SimulatedSensorReader(config))
        elif provider == "http_json":
            readers.append(HttpJsonSensorReader(config))
        elif provider == "tinytuya":
            readers.append(TinyTuyaSensorReader(config))
        elif provider == "mqtt":
            readers.append(MqttSensorReader(config, mqtt_hub))
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


def _to_humidity(raw_value: Any, scale: float, offset: float) -> float:
    return round(float(raw_value) * scale + offset, 1)


def _read_humidity_path(payload: Any, config: SensorConfig) -> float | None:
    if not config.humidity_path:
        return None
    raw_value = _resolve_path(payload, config.humidity_path)
    return _to_humidity(raw_value, config.humidity_scale, config.humidity_offset)


def _read_humidity_dp(dps: dict[str, Any], config: SensorConfig) -> float | None:
    if not config.humidity_dp:
        return None
    raw_value = dps.get(str(config.humidity_dp))
    if raw_value is None:
        return None
    return _to_humidity(raw_value, config.humidity_scale, config.humidity_offset)

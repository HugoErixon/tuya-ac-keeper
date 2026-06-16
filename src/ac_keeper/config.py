from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DatabaseConfig:
    path: Path = Path("data/ac_keeper.sqlite")


@dataclass(frozen=True)
class ApiConfig:
    host: str = "127.0.0.1"
    port: int = 8089


@dataclass(frozen=True)
class ControllerConfig:
    target_c: float = 21.5
    hysteresis_c: float = 0.4
    poll_seconds: int = 30
    min_cycle_seconds: int = 180
    aggregate: str = "average"
    heat_enabled: bool = False
    dry_run: bool = True


@dataclass(frozen=True)
class DpsConfig:
    power: str = "1"
    mode: str = "2"
    target_temperature: str = "3"
    current_temperature: str | None = "4"


@dataclass(frozen=True)
class ModeConfig:
    cool: str = "cold"
    heat: str = "hot"
    auto: str = "auto"
    fan: str = "wind"


@dataclass(frozen=True)
class AcDeviceConfig:
    provider: str = "simulated"
    name: str = "ac"
    device_id: str = ""
    address: str = ""
    local_key: str = ""
    version: str = "3.3"
    temperature_scale: float = 1.0
    dps: DpsConfig = field(default_factory=DpsConfig)
    modes: ModeConfig = field(default_factory=ModeConfig)


@dataclass(frozen=True)
class SensorConfig:
    name: str
    provider: str = "simulated"
    device_id: str = ""
    address: str = ""
    local_key: str = ""
    version: str = "3.3"
    temp_dp: str = "1"
    url: str = ""
    value_path: str = "temperature"
    headers: dict[str, str] = field(default_factory=dict)
    scale: float = 1.0
    offset_c: float = 0.0
    weight: float = 1.0


@dataclass(frozen=True)
class AppConfig:
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    api: ApiConfig = field(default_factory=ApiConfig)
    controller: ControllerConfig = field(default_factory=ControllerConfig)
    ac: AcDeviceConfig = field(default_factory=AcDeviceConfig)
    sensors: list[SensorConfig] = field(default_factory=lambda: [SensorConfig(name="room_simulated")])


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path)
    raw = _load_mapping(config_path)
    raw = _resolve_env(raw)
    base_dir = config_path.parent
    return app_config_from_mapping(raw, base_dir=base_dir)


def app_config_from_mapping(raw: dict[str, Any], base_dir: Path | None = None) -> AppConfig:
    base_dir = base_dir or Path.cwd()
    database_raw = raw.get("database", {})
    api_raw = raw.get("api", {})
    controller_raw = raw.get("controller", {})
    ac_raw = raw.get("ac", {})

    db_path = Path(database_raw.get("path", "data/ac_keeper.sqlite"))
    if not db_path.is_absolute():
        db_path = base_dir / db_path

    dps = DpsConfig(**{**DpsConfig().__dict__, **ac_raw.get("dps", {})})
    modes = ModeConfig(**{**ModeConfig().__dict__, **ac_raw.get("modes", {})})
    ac_values = {k: v for k, v in ac_raw.items() if k not in {"dps", "modes"}}

    sensor_values = raw.get("sensors") or [{"name": "room_simulated"}]

    return AppConfig(
        database=DatabaseConfig(path=db_path),
        api=ApiConfig(**{**ApiConfig().__dict__, **api_raw}),
        controller=ControllerConfig(**{**ControllerConfig().__dict__, **controller_raw}),
        ac=AcDeviceConfig(
            **{**AcDeviceConfig(dps=dps, modes=modes).__dict__, **ac_values, "dps": dps, "modes": modes}
        ),
        sensors=[SensorConfig(**sensor) for sensor in sensor_values],
    )


def _load_mapping(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        return json.loads(text)

    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("YAML config requires PyYAML. Install with `pip install -e .`.") from exc

    loaded = yaml.safe_load(text)
    return loaded or {}


def _resolve_env(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _resolve_env(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_resolve_env(inner) for inner in value]
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        name_default = value[2:-1]
        if ":" in name_default:
            name, default = name_default.split(":", 1)
        else:
            name, default = name_default, ""
        return os.getenv(name, default)
    return value

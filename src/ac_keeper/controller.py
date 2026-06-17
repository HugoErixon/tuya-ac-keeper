from __future__ import annotations

import logging
import statistics
import time
from datetime import datetime, timezone

from .config import AppConfig, SensorConfig
from .db import TemperatureStore
from .domain import AcStatus, ControlDecision, TemperatureReading
from .sensors import SensorReader, build_sensor_readers
from .tuya_client import AcClient, build_ac_client

logger = logging.getLogger(__name__)


class ThermostatController:
    def __init__(
        self,
        config: AppConfig,
        store: TemperatureStore,
        sensors: list[SensorReader],
        ac: AcClient,
    ):
        self.config = config
        self.store = store
        self.sensors = sensors
        self.ac = ac
        self._last_applied_at: datetime | None = None

    def run_once(self) -> ControlDecision:
        readings = self.read_sensors()
        self.store.insert_sensor_readings(readings)

        status = self.ac.status()
        self.store.insert_ac_status(status)

        decision = self.decide(readings, status)
        self.apply(decision, status)
        self.store.insert_control_event(decision)

        logger.info(
            "control action=%s measured=%s target=%s reason=%s",
            decision.action,
            decision.measured_c,
            decision.target_c,
            decision.reason,
        )
        return decision

    def read_sensors(self) -> list[TemperatureReading]:
        readings: list[TemperatureReading] = []
        for sensor in self.sensors:
            try:
                readings.append(sensor.read())
            except Exception:
                logger.exception("sensor read failed")
        return readings

    def decide(self, readings: list[TemperatureReading], status: AcStatus) -> ControlDecision:
        measured = aggregate_temperature(readings, self.config.sensors, self.config.controller.aggregate)
        if measured is None:
            return ControlDecision(
                target_c=self.config.controller.target_c,
                measured_c=None,
                action="no_sensor_data",
                reason="No sensor readings were available.",
            )

        cfg = self.config.controller
        target = cfg.target_c
        hysteresis = cfg.hysteresis_c
        modes = self.config.ac.modes
        above = measured - target

        # På/av styrs HELT av Zigbee-mätningen (AC:ns egen givare används aldrig
        # i beslutet), med hysteres för att undvika kort cykling.
        if above > hysteresis:
            want_cool = True
        elif above < -hysteresis:
            want_cool = False
        else:
            want_cool = bool(status.power)  # inom dödbandet: behåll nuvarande läge

        if want_cool:
            # Proportionell AC-setpoint: full kyla (min_setpoint) när långt över målet,
            # höj mjukt mot målet när rummet närmar sig → snabb start utan översläng.
            span = max(0.0, target - cfg.min_setpoint_c)
            band = cfg.ramp_band_c if cfg.ramp_band_c > 0 else 1.0
            frac = min(1.0, max(0.0, above) / band)
            setpoint = float(round(target - frac * span))
            setpoint = min(target, max(cfg.min_setpoint_c, setpoint))
            requested_power = True
            requested_mode = modes.cool
            requested_setpoint = setpoint
            action = "cool"
            reason = f"Room {measured:.2f}C vs target {target:.1f}C -> cool, AC setpoint {setpoint:.1f}C."
        else:
            requested_power = False
            requested_mode = None
            requested_setpoint = None
            action = "off"
            reason = f"Room {measured:.2f}C at/below target {target:.1f}C -> AC off."

        if self._cycle_locked(status, requested_power, requested_mode):
            return ControlDecision(
                target_c=target,
                measured_c=measured,
                action="defer",
                reason=f"Waiting for min_cycle_seconds={self.config.controller.min_cycle_seconds}.",
                requested_power=requested_power,
                requested_mode=requested_mode,
                requested_setpoint_c=requested_setpoint,
            )

        return ControlDecision(
            target_c=target,
            measured_c=measured,
            action=action if not self.config.controller.dry_run else f"dry_run_{action}",
            reason=reason,
            requested_power=requested_power,
            requested_mode=requested_mode,
            requested_setpoint_c=requested_setpoint,
        )

    def apply(self, decision: ControlDecision, status: AcStatus | None = None) -> None:
        if decision.action.startswith("dry_run_") or decision.action in {"hold", "defer", "no_sensor_data"}:
            return

        power = decision.requested_power
        mode = decision.requested_mode
        setpoint = decision.requested_setpoint_c

        # Skicka bara DP:er som faktiskt ändras — annars piper AC:n vid varje cykel.
        if status is not None:
            if power is not None and power == status.power:
                power = None
            if mode is not None and mode == status.mode:
                mode = None
            if (setpoint is not None and status.target_temperature_c is not None
                    and abs(setpoint - status.target_temperature_c) < 0.5):
                setpoint = None
            if power is None and mode is None and setpoint is None:
                return  # inget har ändrats → skicka inget kommando (inget pip)

        self.ac.apply(power=power, mode=mode, setpoint_c=setpoint)
        self._last_applied_at = datetime.now(timezone.utc)

    def run_forever(self) -> None:
        while True:
            try:
                self.run_once()
            except Exception:
                # Tillfälligt fel (t.ex. onåbar AC eller sensor) får ALDRIG döda
                # loopen — logga och fortsätt till nästa cykel.
                logger.exception("control cycle failed; continuing")
            time.sleep(self.config.controller.poll_seconds)

    def _cycle_locked(self, status: AcStatus, requested_power: bool | None, requested_mode: str | None) -> bool:
        if self._last_applied_at is None:
            return False
        changes_power = requested_power is not None and status.power != requested_power
        changes_mode = requested_mode is not None and status.mode != requested_mode
        if not (changes_power or changes_mode):
            return False
        elapsed = (datetime.now(timezone.utc) - self._last_applied_at).total_seconds()
        return elapsed < self.config.controller.min_cycle_seconds


def build_controller(config: AppConfig) -> ThermostatController:
    store = TemperatureStore(config.database.path)
    sensors = build_sensor_readers(config.sensors)
    ac = build_ac_client(config.ac)
    return ThermostatController(config=config, store=store, sensors=sensors, ac=ac)


def aggregate_temperature(
    readings: list[TemperatureReading],
    sensor_configs: list[SensorConfig],
    method: str,
) -> float | None:
    if not readings:
        return None

    by_name = {config.name: config for config in sensor_configs}
    method = method.lower()

    if method == "median":
        return round(statistics.median(reading.temperature_c for reading in readings), 2)
    if method != "average":
        raise ValueError(f"Unknown aggregate method {method!r}; expected average or median")

    weighted_sum = 0.0
    total_weight = 0.0
    for reading in readings:
        weight = by_name.get(reading.sensor_name, SensorConfig(name=reading.sensor_name)).weight
        weighted_sum += reading.temperature_c * weight
        total_weight += weight
    if total_weight <= 0:
        return None
    return round(weighted_sum / total_weight, 2)

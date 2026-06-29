from __future__ import annotations

import logging
import statistics
import time
import json
from dataclasses import replace
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any
from urllib.request import urlopen
from zoneinfo import ZoneInfo

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
        self._sleep_schedule_cache: tuple[datetime, datetime] | None = None
        self._sleep_schedule_cached_at = 0.0
        self._outdoor_temp_cache: float | None = None
        self._outdoor_temp_cached_at = 0.0
        self._cooling_rate_cache: tuple[float, int] | None = None
        self._cooling_rate_cached_at = 0.0

    def run_once(self) -> ControlDecision:
        readings = self.read_sensors()
        self.store.insert_sensor_readings(readings)

        status = self.ac.status()
        self.store.insert_ac_status(status)

        if self._water_lockout():
            # Vattendunken (kondens) är full — översvämningsskydd. Tvinga AC:n AV
            # varje cykel tills låset släpps manuellt från dashboarden. Detta går
            # FÖRE all vanlig reglering och struntar i min_cycle/keep_cool_on.
            measured = aggregate_temperature(
                readings, self.config.sensors, self.config.controller.aggregate
            )
            dry = self.config.controller.dry_run
            decision = ControlDecision(
                target_c=self.config.controller.target_c,
                measured_c=measured,
                action="dry_run_water_lockout" if dry else "water_lockout",
                reason="Water tank full — forcing AC off to prevent overflow.",
                requested_power=False,
                requested_mode=None,
                requested_setpoint_c=None,
            )
            if status.power and not dry:
                try:
                    self.ac.apply(power=False, mode=None, setpoint_c=None)
                    self._last_applied_at = datetime.now(timezone.utc)
                except Exception:
                    logger.exception("water lockout: failed to power off AC")
            self.store.insert_control_event(decision)
            logger.warning("WATER LOCKOUT active — AC forced off (tank full).")
            return decision

        decision = self.decide(readings, status)
        if self._control_enabled():
            self.apply(decision, status)
        elif decision.measured_c is not None:
            # Styrning avstängd via dashboarden — logga ändå temperaturen, men
            # kommendera inte AC:n.
            decision = replace(
                decision,
                action="disabled",
                reason="AC control disabled from dashboard — logging only.",
                requested_power=None, requested_mode=None, requested_setpoint_c=None,
            )
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
        pre_cool_reason = self._pre_cool_reason(measured, target)
        if pre_cool_reason is not None:
            return ControlDecision(
                target_c=target,
                measured_c=measured,
                action="pre_cool_wait" if not cfg.dry_run else "dry_run_pre_cool_wait",
                reason=pre_cool_reason,
                requested_power=False,
                requested_mode=None,
                requested_setpoint_c=None,
            )

        # På/av styrs HELT av Zigbee-mätningen (AC:ns egen givare används aldrig
        # i beslutet), med hysteres för att undvika kort cykling.
        if above > hysteresis:
            want_cool = True
        elif cfg.keep_cool_on:
            # Keep the unit powered in cool mode overnight; the AC can idle at
            # target instead of the external loop turning it fully off.
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
            action = "cool" if above > hysteresis else "hold_cool"
            reason = (
                f"Room {measured:.2f}C vs target {target:.1f}C -> cool, AC setpoint {setpoint:.1f}C."
                if above > hysteresis
                else f"Room {measured:.2f}C at/below target {target:.1f}C -> keep AC on at target for stable overnight temperature."
            )
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

    def manual_control(self, mode: str, setpoint_c: float | None) -> ControlDecision:
        mode = (mode or "").strip().lower()
        allowed_modes = {"cool", "fan", "auto", "heat", "off"}
        if mode not in allowed_modes:
            raise ValueError("mode must be one of cool, fan, auto, heat, or off")
        if mode != "off":
            if setpoint_c is None:
                raise ValueError("setpoint_c is required unless mode is off")
            if not 10.0 <= setpoint_c <= 35.0:
                raise ValueError("setpoint_c must be between 10 and 35")
            setpoint_c = round(setpoint_c * 2) / 2

        self._set_control_enabled(False)
        readings = self.read_sensors()
        self.store.insert_sensor_readings(readings)
        status = self.ac.status()
        self.store.insert_ac_status(status)
        measured = aggregate_temperature(readings, self.config.sensors, self.config.controller.aggregate)

        if mode == "off":
            power = False
            requested_mode = None
            requested_setpoint = None
            reason = "Manual dashboard command -> AC off; automatic control disabled."
        else:
            mode_map = {
                "cool": self.config.ac.modes.cool,
                "fan": self.config.ac.modes.fan,
                "auto": self.config.ac.modes.auto,
                "heat": self.config.ac.modes.heat,
            }
            power = True
            requested_mode = mode_map[mode]
            requested_setpoint = setpoint_c
            reason = (
                f"Manual dashboard command -> mode {mode}, AC setpoint {setpoint_c:.1f}C; "
                "automatic control disabled."
            )

        if not self.config.controller.dry_run:
            self.ac.apply(power=power, mode=requested_mode, setpoint_c=requested_setpoint)
            self._last_applied_at = datetime.now(timezone.utc)

        decision = ControlDecision(
            target_c=self.config.controller.target_c,
            measured_c=measured,
            action=f"manual_{mode}" if not self.config.controller.dry_run else f"dry_run_manual_{mode}",
            reason=reason,
            requested_power=power,
            requested_mode=requested_mode,
            requested_setpoint_c=requested_setpoint,
        )
        self.store.insert_control_event(decision)
        return decision

    def _control_enabled(self) -> bool:
        """Läser flagg-filen (skrivs av dashboardens toggle). Saknas den → styrning på.
        Loopen loggar ALLTID; flaggan styr bara om AC:n faktiskt kommenderas."""
        flag_path = self.config.database.path.parent / "control_enabled"
        try:
            return flag_path.read_text().strip().lower() not in ("0", "false", "off", "no", "")
        except FileNotFoundError:
            return True
        except Exception:
            return True

    def _set_control_enabled(self, enabled: bool) -> None:
        flag_path = self.config.database.path.parent / "control_enabled"
        flag_path.parent.mkdir(parents=True, exist_ok=True)
        flag_path.write_text("1" if enabled else "0", encoding="utf-8")

    def _water_lockout(self) -> bool:
        """Är vattendunken full? Flagg-filen skrivs av dashboardens /api/water.
        Finns + sann → tvinga AC av. Saknas → inget lås (felsäkert default)."""
        flag_path = self.config.database.path.parent / "water_lockout"
        try:
            return flag_path.read_text().strip().lower() not in ("0", "false", "off", "no", "")
        except FileNotFoundError:
            return False
        except Exception:
            return False

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

    def _pre_cool_reason(self, measured_c: float, target_c: float) -> str | None:
        cfg = self.config.pre_cool
        if not cfg.enabled:
            return None

        schedule = self._sleep_schedule()
        if schedule is None:
            return None

        bedtime, wake_time = schedule
        now = self._now()
        if 0 <= cfg.overnight_hold_until_hour and now.hour < cfg.overnight_hold_until_hour:
            return None
        if now >= bedtime:
            return None

        outdoor_c = self._outdoor_temperature()
        heat_pressure = max(0.0, (outdoor_c or target_c) - target_c) * cfg.outside_heat_factor
        cooling_need_c = max(0.0, measured_c - target_c) + heat_pressure + cfg.sleeper_heat_buffer_c
        rate_c_per_hour, rate_samples = self._cooling_rate_c_per_hour()
        rate_c_per_min = max(0.01, rate_c_per_hour / 60.0)
        lead_minutes = max(float(cfg.min_lead_minutes), cooling_need_c / rate_c_per_min)
        start_at = bedtime - timedelta(minutes=lead_minutes)

        if now >= start_at:
            return None

        outdoor_label = f", outside {outdoor_c:.1f}C" if outdoor_c is not None else ""
        return (
            f"Pre-cool waits until {start_at.strftime('%H:%M')} for bedtime {bedtime.strftime('%H:%M')} "
            f"(wake {wake_time.strftime('%H:%M')}, room {measured_c:.2f}C, target {target_c:.1f}C"
            f"{outdoor_label}, cooling rate {rate_c_per_hour:.2f}C/h from {rate_samples} samples, "
            f"estimated lead {lead_minutes:.0f} min)."
        )

    def _cooling_rate_c_per_hour(self) -> tuple[float, int]:
        cfg = self.config.pre_cool
        fallback = max(0.1, cfg.cooling_rate_c_per_hour)
        if not cfg.calibrate_cooling_rate:
            return fallback, 0

        now_mono = time.monotonic()
        if (
            self._cooling_rate_cache is not None
            and now_mono - self._cooling_rate_cached_at < cfg.calibration_refresh_seconds
        ):
            return self._cooling_rate_cache

        try:
            samples = self.store.cooling_rate_samples(hours=cfg.calibration_history_hours)
            rates = [sample["rate_c_per_hour"] for sample in samples]
            if len(rates) >= cfg.calibration_min_samples:
                rate = float(statistics.median(rates))
                self._cooling_rate_cache = (rate, len(rates))
            else:
                self._cooling_rate_cache = (fallback, len(rates))
            self._cooling_rate_cached_at = now_mono
            return self._cooling_rate_cache
        except Exception:
            logger.exception("cooling-rate calibration unavailable")
            return self._cooling_rate_cache or (fallback, 0)

    def _sleep_schedule(self) -> tuple[datetime, datetime] | None:
        cfg = self.config.pre_cool
        now_mono = time.monotonic()
        if (
            self._sleep_schedule_cache is not None
            and now_mono - self._sleep_schedule_cached_at < cfg.schedule_refresh_seconds
        ):
            return self._sleep_schedule_cache

        manual_bedtime = self._manual_bedtime_override()
        try:
            data = _http_json(f"{cfg.dashboard_url.rstrip('/')}/api/sleep-coach", timeout=5)
            night = (data.get("night") or {})
            bedtime = _sleep_clock_to_datetime(
                wake_date=night.get("date"),
                bedtime_clock=manual_bedtime or night.get("bedtime"),
                wake_clock=night.get("wake"),
                tz_name=cfg.timezone,
            )
            wake_time = _clock_on_date(night.get("date"), night.get("wake"), cfg.timezone)
            self._sleep_schedule_cache = (bedtime, wake_time)
            self._sleep_schedule_cached_at = now_mono
            return self._sleep_schedule_cache
        except Exception:
            logger.exception("pre-cool schedule unavailable")
            if manual_bedtime:
                bedtime = _next_clock_datetime(manual_bedtime, cfg.timezone)
                wake_time = bedtime + timedelta(hours=8)
                self._sleep_schedule_cache = (bedtime, wake_time)
                self._sleep_schedule_cached_at = now_mono
                return self._sleep_schedule_cache
            return self._sleep_schedule_cache

    def _manual_bedtime_override(self) -> str | None:
        cfg = self.config.pre_cool
        try:
            data = _http_json(f"{cfg.dashboard_url.rstrip('/')}/api/ac/bedtime", timeout=5)
            bedtime = data.get("bedtime")
            if isinstance(bedtime, str) and _is_clock(bedtime):
                return bedtime
        except Exception:
            logger.exception("manual bedtime override unavailable")
        return None

    def _outdoor_temperature(self) -> float | None:
        cfg = self.config.pre_cool
        now_mono = time.monotonic()
        if (
            self._outdoor_temp_cache is not None
            and now_mono - self._outdoor_temp_cached_at < cfg.weather_refresh_seconds
        ):
            return self._outdoor_temp_cache

        try:
            data = _http_json(f"{cfg.dashboard_url.rstrip('/')}/api/weather/current", timeout=5)
            temp = data.get("temperature_c")
            self._outdoor_temp_cache = float(temp) if temp is not None else None
            self._outdoor_temp_cached_at = now_mono
            return self._outdoor_temp_cache
        except Exception:
            logger.exception("pre-cool weather unavailable")
            return self._outdoor_temp_cache

    def _now(self) -> datetime:
        return datetime.now(ZoneInfo(self.config.pre_cool.timezone))


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


def _http_json(url: str, timeout: float) -> dict[str, Any]:
    with urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _sleep_clock_to_datetime(
    wake_date: str | None,
    bedtime_clock: str | None,
    wake_clock: str | None,
    tz_name: str,
) -> datetime:
    if not wake_date or not bedtime_clock or not wake_clock:
        raise ValueError("sleep schedule is missing date, bedtime, or wake")
    wake_day = date.fromisoformat(wake_date)
    bed_minutes = _clock_minutes(bedtime_clock)
    wake_minutes = _clock_minutes(wake_clock)
    bed_day = wake_day - timedelta(days=1) if bed_minutes > wake_minutes else wake_day
    return _clock_on_date(bed_day.isoformat(), bedtime_clock, tz_name)


def _clock_on_date(day: str | None, clock: str | None, tz_name: str) -> datetime:
    if not day or not clock:
        raise ValueError("date and clock are required")
    hour, minute = [int(part) for part in clock.split(":", 1)]
    return datetime.combine(date.fromisoformat(day), dt_time(hour, minute), ZoneInfo(tz_name))


def _clock_minutes(clock: str) -> int:
    hour, minute = [int(part) for part in clock.split(":", 1)]
    return hour * 60 + minute


def _is_clock(clock: str) -> bool:
    try:
        hour, minute = [int(part) for part in clock.split(":", 1)]
    except Exception:
        return False
    return 0 <= hour <= 23 and 0 <= minute <= 59


def _next_clock_datetime(clock: str, tz_name: str) -> datetime:
    hour, minute = [int(part) for part in clock.split(":", 1)]
    now = datetime.now(ZoneInfo(tz_name))
    candidate = datetime.combine(now.date(), dt_time(hour, minute), ZoneInfo(tz_name))
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate

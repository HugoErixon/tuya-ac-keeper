from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ac_keeper.config import AppConfig, ControllerConfig, PreCoolConfig, SensorConfig
from ac_keeper.controller import ThermostatController, aggregate_temperature
from ac_keeper.db import TemperatureStore
from ac_keeper.domain import AcStatus, ControlDecision, TemperatureReading
from ac_keeper.tuya_client import SimulatedAcClient


class ControllerTests(unittest.TestCase):
    def test_weighted_average(self) -> None:
        readings = [
            TemperatureReading("near_bed", 22.0),
            TemperatureReading("near_window", 20.0),
        ]
        sensors = [
            SensorConfig(name="near_bed", weight=3.0),
            SensorConfig(name="near_window", weight=1.0),
        ]

        self.assertEqual(aggregate_temperature(readings, sensors, "average"), 21.5)

    def test_cools_when_above_band(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(
                database=AppConfig().database,
                controller=ControllerConfig(target_c=21.0, hysteresis_c=0.3, dry_run=True),
                sensors=[SensorConfig(name="room")],
            )
            store = TemperatureStore(Path(tmp) / "db.sqlite")
            controller = ThermostatController(config, store, sensors=[], ac=SimulatedAcClient(config.ac))

            decision = controller.decide(
                [TemperatureReading("room", 21.6)],
                AcStatus(power=False, mode="cold", target_temperature_c=21.0, current_temperature_c=22.0),
            )

            self.assertEqual(decision.action, "dry_run_cool")
            self.assertTrue(decision.requested_power)

    def test_no_sensor_data_holds(self) -> None:
        config = AppConfig(controller=ControllerConfig(target_c=21.0))
        with tempfile.TemporaryDirectory() as tmp:
            controller = ThermostatController(
                config,
                TemperatureStore(Path(tmp) / "db.sqlite"),
                sensors=[],
                ac=SimulatedAcClient(config.ac),
            )

            decision = controller.decide(
                [],
                AcStatus(power=False, mode=None, target_temperature_c=None, current_temperature_c=None),
            )

            self.assertEqual(decision.action, "no_sensor_data")

    def test_pre_cool_waits_before_calculated_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(
                database=AppConfig().database,
                controller=ControllerConfig(target_c=18.0, hysteresis_c=0.3, dry_run=False),
                pre_cool=PreCoolConfig(enabled=True, cooling_rate_c_per_hour=1.2, sleeper_heat_buffer_c=0.5),
                sensors=[SensorConfig(name="room")],
            )
            controller = ThermostatController(
                config,
                TemperatureStore(Path(tmp) / "db.sqlite"),
                sensors=[],
                ac=SimulatedAcClient(config.ac),
            )
            now = datetime.now(ZoneInfo("Europe/Stockholm"))
            controller._sleep_schedule_cache = (now + timedelta(hours=4), now + timedelta(hours=12))
            controller._sleep_schedule_cached_at = time.monotonic()
            controller._outdoor_temp_cache = 20.0
            controller._outdoor_temp_cached_at = time.monotonic()

            decision = controller.decide(
                [TemperatureReading("room", 20.0)],
                AcStatus(power=False, mode="cold", target_temperature_c=18.0, current_temperature_c=20.0),
            )

            self.assertEqual(decision.action, "pre_cool_wait")
            self.assertFalse(decision.requested_power)

    def test_pre_cool_allows_cooling_after_calculated_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(
                database=AppConfig().database,
                controller=ControllerConfig(target_c=18.0, hysteresis_c=0.3, dry_run=False),
                pre_cool=PreCoolConfig(enabled=True, cooling_rate_c_per_hour=1.2, sleeper_heat_buffer_c=0.5),
                sensors=[SensorConfig(name="room")],
            )
            controller = ThermostatController(
                config,
                TemperatureStore(Path(tmp) / "db.sqlite"),
                sensors=[],
                ac=SimulatedAcClient(config.ac),
            )
            now = datetime.now(ZoneInfo("Europe/Stockholm"))
            controller._sleep_schedule_cache = (now + timedelta(minutes=30), now + timedelta(hours=8))
            controller._sleep_schedule_cached_at = time.monotonic()
            controller._outdoor_temp_cache = 20.0
            controller._outdoor_temp_cached_at = time.monotonic()

            decision = controller.decide(
                [TemperatureReading("room", 20.0)],
                AcStatus(power=False, mode="cold", target_temperature_c=18.0, current_temperature_c=20.0),
            )

            self.assertEqual(decision.action, "cool")
            self.assertTrue(decision.requested_power)

    def test_cooling_rate_uses_historical_median(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = AppConfig(
                pre_cool=PreCoolConfig(
                    enabled=True,
                    cooling_rate_c_per_hour=1.2,
                    calibration_min_samples=2,
                    calibration_refresh_seconds=1800,
                )
            )
            store = TemperatureStore(Path(tmp) / "db.sqlite")
            base = datetime.now(timezone.utc) - timedelta(hours=4)
            for start, end, minutes in [(22.0, 21.0, 60), (22.0, 20.0, 60), (22.0, 20.5, 60)]:
                store.insert_control_event(ControlDecision(
                    target_c=18.0,
                    measured_c=start,
                    action="cool",
                    reason="test",
                    requested_power=True,
                    created_at=base,
                ))
                store.insert_control_event(ControlDecision(
                    target_c=18.0,
                    measured_c=end,
                    action="cool",
                    reason="test",
                    requested_power=True,
                    created_at=base + timedelta(minutes=minutes),
                ))
                store.insert_control_event(ControlDecision(
                    target_c=18.0,
                    measured_c=end,
                    action="off",
                    reason="test",
                    requested_power=False,
                    created_at=base + timedelta(minutes=minutes + 1),
                ))
                base += timedelta(hours=2)

            controller = ThermostatController(config, store, sensors=[], ac=SimulatedAcClient(config.ac))

            self.assertEqual(controller._cooling_rate_c_per_hour(), (1.5, 3))


if __name__ == "__main__":
    unittest.main()

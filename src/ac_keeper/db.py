from __future__ import annotations

import csv
import io
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .domain import AcStatus, ControlDecision, TemperatureReading


class TemperatureStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sensor_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    sensor_name TEXT NOT NULL,
                    temperature_c REAL NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_sensor_readings_ts
                    ON sensor_readings(ts);
                CREATE INDEX IF NOT EXISTS idx_sensor_readings_sensor_ts
                    ON sensor_readings(sensor_name, ts);

                CREATE TABLE IF NOT EXISTS ac_status (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    power INTEGER,
                    mode TEXT,
                    target_temperature_c REAL,
                    current_temperature_c REAL,
                    raw_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_ac_status_ts
                    ON ac_status(ts);

                CREATE TABLE IF NOT EXISTS control_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    target_c REAL NOT NULL,
                    measured_c REAL,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    requested_power INTEGER,
                    requested_mode TEXT,
                    requested_setpoint_c REAL
                );
                CREATE INDEX IF NOT EXISTS idx_control_events_ts
                    ON control_events(ts);
                """
            )

    def insert_sensor_readings(self, readings: list[TemperatureReading]) -> None:
        if not readings:
            return
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO sensor_readings (ts, sensor_name, temperature_c, raw_json)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        reading.observed_at.isoformat(),
                        reading.sensor_name,
                        reading.temperature_c,
                        json.dumps(reading.raw, sort_keys=True),
                    )
                    for reading in readings
                ],
            )

    def insert_ac_status(self, status: AcStatus) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO ac_status
                    (ts, power, mode, target_temperature_c, current_temperature_c, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    status.observed_at.isoformat(),
                    _bool_to_int(status.power),
                    status.mode,
                    status.target_temperature_c,
                    status.current_temperature_c,
                    json.dumps(status.raw, sort_keys=True),
                ),
            )

    def insert_control_event(self, decision: ControlDecision) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO control_events
                    (ts, target_c, measured_c, action, reason, requested_power, requested_mode, requested_setpoint_c)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.created_at.isoformat(),
                    decision.target_c,
                    decision.measured_c,
                    decision.action,
                    decision.reason,
                    _bool_to_int(decision.requested_power),
                    decision.requested_mode,
                    decision.requested_setpoint_c,
                ),
            )

    def current_snapshot(self) -> dict[str, Any]:
        return {
            "latest_readings": self.latest_sensor_readings(),
            "latest_ac_status": self.latest_ac_status(),
            "latest_control_event": self.latest_control_event(),
        }

    def latest_sensor_readings(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT sr.*
                FROM sensor_readings sr
                JOIN (
                    SELECT sensor_name, MAX(ts) AS max_ts
                    FROM sensor_readings
                    GROUP BY sensor_name
                ) latest
                  ON latest.sensor_name = sr.sensor_name AND latest.max_ts = sr.ts
                ORDER BY sr.sensor_name
                """
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def latest_ac_status(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM ac_status ORDER BY ts DESC LIMIT 1").fetchone()
        return _row_to_dict(row) if row else None

    def latest_control_event(self) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM control_events ORDER BY ts DESC LIMIT 1").fetchone()
        return _row_to_dict(row) if row else None

    def sensor_series(self, hours: float = 24.0, sensor_name: str | None = None) -> list[dict[str, Any]]:
        since = _since(hours)
        params: list[Any] = [since.isoformat()]
        where = "WHERE ts >= ?"
        if sensor_name:
            where += " AND sensor_name = ?"
            params.append(sensor_name)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT ts, sensor_name, temperature_c
                FROM sensor_readings
                {where}
                ORDER BY ts ASC
                """,
                params,
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def control_events(self, hours: float = 24.0) -> list[dict[str, Any]]:
        since = _since(hours)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT ts, target_c, measured_c, action, reason, requested_power, requested_mode, requested_setpoint_c
                FROM control_events
                WHERE ts >= ?
                ORDER BY ts ASC
                """,
                (since.isoformat(),),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def export_csv(self, hours: float = 24.0) -> str:
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=["ts", "kind", "name", "temperature_c", "action", "target_c"])
        writer.writeheader()
        for row in self.sensor_series(hours=hours):
            writer.writerow(
                {
                    "ts": row["ts"],
                    "kind": "sensor",
                    "name": row["sensor_name"],
                    "temperature_c": row["temperature_c"],
                    "action": "",
                    "target_c": "",
                }
            )
        for row in self.control_events(hours=hours):
            writer.writerow(
                {
                    "ts": row["ts"],
                    "kind": "control",
                    "name": "",
                    "temperature_c": row["measured_c"],
                    "action": row["action"],
                    "target_c": row["target_c"],
                }
            )
        return output.getvalue()


def _since(hours: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _bool_to_int(value: bool | None) -> int | None:
    if value is None:
        return None
    return 1 if value else 0


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    if "power" in data and data["power"] is not None:
        data["power"] = bool(data["power"])
    if "requested_power" in data and data["requested_power"] is not None:
        data["requested_power"] = bool(data["requested_power"])
    if "raw_json" in data:
        data["raw"] = json.loads(data.pop("raw_json") or "{}")
    return data

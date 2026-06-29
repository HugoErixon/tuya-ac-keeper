from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Response

from .config import AppConfig, load_config
from .controller import ThermostatController, build_controller
from .db import TemperatureStore


def create_app(config: AppConfig | None = None) -> FastAPI:
    config = config or load_config(Path(os.getenv("AC_KEEPER_CONFIG", "config.yaml")))
    controller = build_controller(config)
    app = FastAPI(title="Tuya AC Keeper", version="0.1.0")

    app.state.config = config
    app.state.controller = controller
    app.state.store = controller.store

    @app.get("/health")
    def health() -> dict[str, object]:
        return {"ok": True, "database": str(app.state.config.database.path)}

    @app.get("/api/current")
    def current() -> dict[str, object]:
        store: TemperatureStore = app.state.store
        return store.current_snapshot()

    @app.get("/api/readings")
    def readings(hours: float = Query(default=24.0, gt=0), sensor: str | None = None) -> list[dict[str, object]]:
        store: TemperatureStore = app.state.store
        return store.sensor_series(hours=hours, sensor_name=sensor)

    @app.get("/api/control-events")
    def control_events(hours: float = Query(default=24.0, gt=0)) -> list[dict[str, object]]:
        store: TemperatureStore = app.state.store
        return store.control_events(hours=hours)

    @app.get("/api/export.csv")
    def export_csv(hours: float = Query(default=24.0, gt=0)) -> Response:
        store: TemperatureStore = app.state.store
        return Response(content=store.export_csv(hours=hours), media_type="text/csv")

    @app.post("/api/control/once")
    def control_once() -> dict[str, object]:
        controller: ThermostatController = app.state.controller
        decision = controller.run_once()
        return {
            "target_c": decision.target_c,
            "measured_c": decision.measured_c,
            "action": decision.action,
            "reason": decision.reason,
            "requested_power": decision.requested_power,
            "requested_mode": decision.requested_mode,
            "requested_setpoint_c": decision.requested_setpoint_c,
        }

    @app.post("/api/manual-control")
    def manual_control(payload: dict[str, object]) -> dict[str, object]:
        controller: ThermostatController = app.state.controller
        mode = str(payload.get("mode") or "").strip().lower()
        setpoint_raw = payload.get("setpoint_c")
        try:
            setpoint_c = None if setpoint_raw is None else float(setpoint_raw)
            decision = controller.manual_control(mode=mode, setpoint_c=setpoint_c)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {
            "target_c": decision.target_c,
            "measured_c": decision.measured_c,
            "action": decision.action,
            "reason": decision.reason,
            "requested_power": decision.requested_power,
            "requested_mode": decision.requested_mode,
            "requested_setpoint_c": decision.requested_setpoint_c,
            "automatic_enabled": False,
        }

    return app

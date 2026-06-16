from __future__ import annotations

import argparse
import logging
import os
import shutil
from pathlib import Path

from .config import load_config
from .controller import build_controller


def main() -> None:
    parser = argparse.ArgumentParser(prog="ac-keeper")
    parser.add_argument("--config", default="config.yaml", help="Path to YAML/JSON config.")
    parser.add_argument("--log-level", default="INFO", help="Python logging level.")

    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("once", help="Run one control iteration.")
    subparsers.add_parser("run", help="Run the control loop forever.")
    subparsers.add_parser("init-config", help="Create config.yaml from config.example.yaml.")

    api_parser = subparsers.add_parser("api", help="Run the HTTP API.")
    api_parser.add_argument("--host", default=None)
    api_parser.add_argument("--port", type=int, default=None)

    args = parser.parse_args()
    logging.basicConfig(level=getattr(logging, args.log_level.upper()), format="%(asctime)s %(levelname)s %(message)s")

    if args.command == "init-config":
        _init_config(Path(args.config))
        return

    config = load_config(args.config)
    if args.command == "once":
        decision = build_controller(config).run_once()
        print(
            f"action={decision.action} measured={decision.measured_c} "
            f"target={decision.target_c} reason={decision.reason}"
        )
        return

    if args.command == "run":
        build_controller(config).run_forever()
        return

    if args.command == "api":
        import uvicorn

        host = args.host or config.api.host
        port = args.port or config.api.port
        os.environ["AC_KEEPER_CONFIG"] = str(Path(args.config).resolve())
        uvicorn.run("ac_keeper.api:create_app", factory=True, host=host, port=port)
        return

    raise AssertionError(f"Unhandled command {args.command}")


def _init_config(target: Path) -> None:
    if target.exists():
        raise FileExistsError(f"{target} already exists")
    example = Path(__file__).resolve().parents[2] / "config.example.yaml"
    shutil.copyfile(example, target)
    print(f"Created {target}")


if __name__ == "__main__":
    main()

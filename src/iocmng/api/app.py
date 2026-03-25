"""FastAPI application factory for the IOC Manager REST service."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import yaml
from fastapi import FastAPI

from iocmng.api.routes import router, set_controller
from iocmng.core.controller import IocMngController


def _load_yaml(path: str):
    try:
        with open(path, "r") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def create_app(
    config_path: Optional[str] = None,
    beamline_path: Optional[str] = None,
    plugins_dir: Optional[str] = None,
    plugins_config_path: Optional[str] = None,
    disable_ophyd: bool = False,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        config_path: Path to config.yaml (optional).
        beamline_path: Path to values.yaml beamline configuration (optional).
        plugins_dir: Directory for cloned plugins.
        plugins_config_path: Path to a YAML file listing plugins to load on
            startup (see ``IOCMNG_PLUGINS_CONFIG`` env var).  Each entry may
            contain: ``name``, ``git_url``, ``path``, ``branch``, ``pat``,
            ``auto_start``, ``parameters``.
        disable_ophyd: Skip ophyd initialization (default False for API mode).

    Returns:
        Configured FastAPI instance.
    """
    config = _load_yaml(config_path) if config_path else {}
    beamline_config = _load_yaml(beamline_path) if beamline_path else {}
    initial_plugins = _load_yaml(plugins_config_path).get("plugins", []) if plugins_config_path else []

    p_dir = Path(plugins_dir) if plugins_dir else None
    controller = IocMngController(
        config=config,
        beamline_config=beamline_config,
        plugins_dir=p_dir,
        disable_ophyd=disable_ophyd,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        import iocmng
        logging.info(f"IOC Manager v{iocmng.__version__} starting up")
        # Load initial plugins defined in IOCMNG_PLUGINS_CONFIG, then merge
        # with persisted autostart plugins uploaded through REST API.
        persisted_plugins = controller.load_persisted_autostart_plugins()
        startup_plugins = list(initial_plugins)
        seen = {p.get("name") for p in startup_plugins if p.get("name")}
        for p in persisted_plugins:
            name = p.get("name")
            if name and name not in seen:
                startup_plugins.append(p)
                seen.add(name)

        if startup_plugins:
            logging.info(f"Loading {len(startup_plugins)} startup plugin(s)")
            results = controller.add_plugins_from_config(startup_plugins)
            failed = [r for r in results if not r["ok"]]
            if failed:
                logging.warning(f"{len(failed)} initial plugin(s) failed to load: "
                                + ", ".join(r['name'] for r in failed))
        yield
        logging.info("IOC Manager shutting down")
        controller.stop_all()

    app = FastAPI(
        title="IOC Manager",
        description="REST API for dynamically loading and managing IOC tasks and jobs",
        version="2.2.0",
        lifespan=lifespan,
    )
    set_controller(controller)
    app.include_router(router)

    return app


def run_server():
    """Entry point to run the API server."""
    import uvicorn

    config_path = os.environ.get("IOCMNG_CONFIG", None)
    beamline_path = os.environ.get("IOCMNG_BEAMLINE_CONFIG", None)
    plugins_dir = os.environ.get("IOCMNG_PLUGINS_DIR", "/data/plugins")
    plugins_config_path = os.environ.get("IOCMNG_PLUGINS_CONFIG", None)
    host = os.environ.get("IOCMNG_HOST", "0.0.0.0")
    port = int(os.environ.get("IOCMNG_PORT", "8080"))
    disable_ophyd = os.environ.get("IOCMNG_DISABLE_OPHYD", "false").lower() == "true"

    log_level = os.environ.get("IOCMNG_LOG_LEVEL", "info").lower()
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    # Configure our own loggers *before* uvicorn starts.  We pass
    # ``log_config=None`` so uvicorn does NOT overwrite the root logger
    # with its own dictConfig.  This preserves DEBUG-level output for
    # all ``iocmng.*`` loggers.
    logging.basicConfig(
        level=numeric_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        force=True,
    )

    app = create_app(
        config_path=config_path,
        beamline_path=beamline_path,
        plugins_dir=plugins_dir,
        plugins_config_path=plugins_config_path,
        disable_ophyd=disable_ophyd,
    )
    uvicorn.run(app, host=host, port=port, log_level=log_level, log_config=None)

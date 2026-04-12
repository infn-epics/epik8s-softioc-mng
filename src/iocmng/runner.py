"""Standalone soft IOC runner — no REST API server.

This module provides :func:`run_ioc` which is the main entry-point for
applications that want to create a soft IOC process directly from a
:class:`~iocmng.base.task.TaskBase` or :class:`~iocmng.base.job.JobBase`
subclass, without the IOC Manager REST service.

Typical usage from a Python script::

    from iocmng.runner import run_ioc
    from my_plugin import MyTask

    run_ioc(MyTask, config="config.yaml", prefix="MY:IOC")

Or via the CLI::

    iocmng-run --module my_plugin --class MyTask --config config.yaml --prefix MY:IOC
"""

from __future__ import annotations

import importlib
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Type, Union

import yaml

from iocmng.base.job import JobBase
from iocmng.base.task import TaskBase
from iocmng.core.plugin_spec import PluginSpec

logger = logging.getLogger(__name__)

# Sentinel used to block the main thread until SIGINT / SIGTERM.
_shutdown = False


def _signal_handler(sig, frame):
    global _shutdown
    logger.info("Received signal %s — shutting down", sig)
    _shutdown = True


def _load_yaml(path: Union[str, Path]) -> Dict[str, Any]:
    with open(path, "r") as fh:
        return yaml.safe_load(fh) or {}


def _resolve_class(
    module_name: str, class_name: Optional[str] = None
) -> Type[Union[TaskBase, JobBase]]:
    """Import *module_name* and return the requested (or auto-detected) class."""
    mod = importlib.import_module(module_name)
    if class_name:
        cls = getattr(mod, class_name, None)
        if cls is None:
            raise ValueError(f"Class {class_name!r} not found in module {module_name!r}")
        return cls
    # Auto-detect: find the first concrete TaskBase/JobBase subclass.
    for attr_name in dir(mod):
        obj = getattr(mod, attr_name)
        if (
            isinstance(obj, type)
            and issubclass(obj, (TaskBase, JobBase))
            and obj not in (TaskBase, JobBase)
        ):
            return obj
    raise ValueError(
        f"No TaskBase/JobBase subclass found in module {module_name!r}"
    )


def _init_softioc(instance):
    """Build PVs and start the softioc runtime.

    Separated from :func:`run_ioc` so tests can mock this single call
    instead of dealing with ``softioc.builder``/``softioc.softioc`` directly.
    """
    from softioc import softioc, builder

    builder.SetDeviceName(instance.pv_prefix)
    instance.build_pvs()
    builder.LoadDatabase()
    softioc.iocInit()


def run_ioc(
    plugin_class: Type[Union[TaskBase, JobBase]],
    *,
    config: Optional[Union[str, Path, Dict[str, Any]]] = None,
    parameters: Optional[Dict[str, Any]] = None,
    prefix: Optional[str] = None,
    pva: bool = True,
    name: Optional[str] = None,
    pvout: Optional[Union[str, Path]] = None,
) -> None:
    """Run a soft IOC for a single task or job — blocking.

    This is the **library API** for standalone applications.
    It builds the soft IOC PVs, starts the ``softioc`` dispatcher,
    initialises the ``pv_client`` with the chosen provider, runs the
    plugin, and blocks until the process receives ``SIGINT``/``SIGTERM``.

    Args:
        plugin_class: A concrete subclass of :class:`TaskBase` or :class:`JobBase`.
        config: Path to a ``config.yaml`` file **or** an already loaded dict.
            If *None* the plugin is created with default/empty config.
        parameters: Extra parameters merged on top of those in *config*.
        prefix: PV prefix. Overrides the one in *config* if given.
        pva: If *True* (default) initialise ``pv_client`` with PVA;
            *False* for Channel Access.
        name: IOC name.  Defaults to the class name in lower-case.
        pvout: Path to write the list of PV names (one per line).
    """
    global _shutdown
    _shutdown = False

    # ── Resolve configuration ────────────────────────────────────────
    if isinstance(config, (str, Path)):
        raw_config = _load_yaml(config)
    elif isinstance(config, dict):
        raw_config = dict(config)
    else:
        raw_config = {}

    ioc_name = name or plugin_class.__name__.lower()
    plugin_spec = PluginSpec.from_config(
        config=raw_config,
        parameters_override=parameters,
        default_prefix=ioc_name.upper(),
    )

    # ── Initialise pv_client ─────────────────────────────────────────
    from iocmng.core import pv_client
    pv_client.init(pva=pva)

    # ── Instantiate the plugin ───────────────────────────────────────
    is_task = issubclass(plugin_class, TaskBase)

    instance = plugin_class(
        name=ioc_name,
        parameters=dict(plugin_spec.parameters),
        pv_definitions=plugin_spec.pv_definitions,
        prefix=prefix,
        plugin_prefix=plugin_spec.prefix,
        plugin_spec=plugin_spec,
    )

    # ── Build soft IOC records ───────────────────────────────────────
    _init_softioc(instance)

    logger.info(
        "Soft IOC started: name=%s prefix=%s class=%s provider=%s",
        ioc_name,
        instance.pv_prefix,
        plugin_class.__name__,
        pv_client.get_provider(),
    )

    # ── Optionally dump PV list ──────────────────────────────────────
    if pvout:
        pv_names = [f"{instance.pv_prefix}:{pv}" for pv in instance.pvs.keys()]
        Path(pvout).parent.mkdir(parents=True, exist_ok=True)
        Path(pvout).write_text("\n".join(sorted(pv_names)) + "\n")
        logger.info("Wrote %d PVs to %s", len(pv_names), pvout)

    # ── Register signal handlers ─────────────────────────────────────
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    # ── Run ──────────────────────────────────────────────────────────
    try:
        if is_task:
            instance.initialize()
            instance.start()
            # Block main thread until shutdown signal.
            while not _shutdown and instance.running:
                time.sleep(0.5)
            instance.stop()
        else:
            # Job: run once, but keep the IOC alive so PVs remain readable.
            result = instance.run()
            logger.info("Job result: %s", result.to_dict())
            while not _shutdown:
                time.sleep(0.5)
    finally:
        pv_client.close()
        logger.info("IOC %s shut down", ioc_name)


# ------------------------------------------------------------------
# CLI entry-point
# ------------------------------------------------------------------

def main() -> None:
    """``iocmng-run`` CLI entry-point."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="iocmng-run",
        description="Run a standalone soft IOC from an iocmng TaskBase/JobBase class.",
    )
    parser.add_argument(
        "-m", "--module",
        required=True,
        help="Python module containing the plugin class (e.g. 'my_plugin').",
    )
    parser.add_argument(
        "-c", "--class-name",
        default=None,
        help="Class name inside the module (auto-detected if omitted).",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to config.yaml.",
    )
    parser.add_argument(
        "--prefix",
        default=None,
        help="PV prefix override.",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="IOC name (defaults to class name).",
    )
    parser.add_argument(
        "--pva",
        default="true",
        choices=["true", "false"],
        help="Use PVA (true) or CA (false). Default: true.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level. Default: INFO.",
    )
    parser.add_argument(
        "--pvout",
        default=None,
        help="Path to write PV list (one PV name per line).",
    )
    parser.add_argument(
        "-p", "--param",
        action="append",
        metavar="KEY=VALUE",
        default=[],
        help="Extra parameter (key=value). Can be repeated.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Parse extra parameters.
    extra_params: Dict[str, Any] = {}
    for kv in args.param:
        if "=" not in kv:
            parser.error(f"Invalid parameter format: {kv!r} (expected KEY=VALUE)")
        key, val = kv.split("=", 1)
        # Attempt numeric conversion.
        try:
            val = int(val)
        except ValueError:
            try:
                val = float(val)
            except ValueError:
                if val.lower() in ("true", "false"):
                    val = val.lower() == "true"
        extra_params[key] = val

    # Ensure the CWD is on sys.path so ``--module my_plugin`` works when
    # running from the plugin directory.
    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    plugin_class = _resolve_class(args.module, args.class_name)

    run_ioc(
        plugin_class,
        config=args.config,
        parameters=extra_params or None,
        prefix=args.prefix,
        pva=args.pva == "true",
        name=args.name,
        pvout=args.pvout,
    )

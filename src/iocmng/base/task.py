"""
Base class for continuous tasks in the IOC Manager framework.

A task is a long-running process that executes repeatedly in a loop.
User applications should subclass TaskBase and implement the required
abstract methods: initialize(), execute(), and cleanup().
"""

import datetime
import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class TaskBase(ABC):
    """Abstract base class for all continuous IOC Manager tasks.

    A task runs continuously in its own thread, executing the `execute()` method
    on each cycle. Tasks can optionally integrate with EPICS soft IOC PVs when
    a PV provider is configured.

    Subclasses must implement:
        - initialize(): one-time setup
        - execute(): called each cycle
        - cleanup(): teardown when stopping

    Example::

        from iocmng import TaskBase

        class MyMonitor(TaskBase):
            def initialize(self):
                self.logger.info("Starting monitor")

            def execute(self):
                # do monitoring work
                self.set_pv("VALUE", 42.0)

            def cleanup(self):
                self.logger.info("Stopping monitor")
    """

    # Class-level marker used for validation
    _iocmng_type = "task"

    def __init__(
        self,
        name: str,
        parameters: Optional[Dict[str, Any]] = None,
        pv_definitions: Optional[Dict[str, Any]] = None,
        beamline_config: Optional[Dict[str, Any]] = None,
        ophyd_devices: Optional[Dict[str, object]] = None,
        prefix: Optional[str] = None,
        device_resolver: Optional[Any] = None,
    ):
        """Initialize a task.

        Args:
            name: Unique task name.
            parameters: Task-specific parameters.
            pv_definitions: PV definitions (inputs/outputs) for soft IOC integration.
            beamline_config: Full beamline configuration dict.
            ophyd_devices: Dictionary of Ophyd device instances.
            prefix: PV prefix (overrides beamline_config).
            device_resolver: Optional callable(name) -> device for lazy creation.
        """
        self.name = name
        self.parameters = parameters or {}
        self.pv_definitions = pv_definitions or {}
        self.beamline_config = beamline_config or {}
        self.ophyd_devices = ophyd_devices or {}
        self._device_resolver = device_resolver
        self.logger = logging.getLogger(f"iocmng.task.{name}")

        # PV storage
        self.pvs: Dict[str, Any] = {}

        # Task control
        self.enabled = True
        self.running = False
        self.task_lock = threading.Lock()

        # PV prefix
        self.pv_prefix = self._get_pv_prefix(prefix)

        # Task mode: 'continuous' (default) or 'triggered'
        mode = self.parameters.get("mode") or (
            "triggered" if self.parameters.get("triggered") else "continuous"
        )
        self.mode = str(mode).lower() if isinstance(mode, str) else "continuous"
        if self.mode not in ("continuous", "triggered"):
            self.mode = "continuous"

        # Cycle counter
        self.cycle_count = 0
        self._trigger_thread = None
        self._thread: Optional[threading.Thread] = None

    def _get_pv_prefix(self, controller_prefix: Optional[str] = None) -> str:
        if controller_prefix:
            return f"{controller_prefix}:{self.name.upper()}"
        beamline = self.beamline_config.get("beamline", "BEAMLINE")
        namespace = self.beamline_config.get("namespace", "DEFAULT")
        return f"{beamline.upper()}:{namespace.upper()}:{self.name.upper()}"

    # ------------------------------------------------------------------
    # Soft IOC PV integration
    # ------------------------------------------------------------------

    def build_pvs(self):
        """Build PVs using softioc builder.

        Creates the default control PVs (ENABLE, STATUS, MESSAGE, etc.)
        plus any PVs defined in pv_definitions from the plugin config YAML.
        Called by the controller before IOC initialization.
        """
        from softioc import builder

        builder.SetDeviceName(self.pv_prefix)

        self.pvs["ENABLE"] = builder.boolOut(
            "ENABLE",
            initial_value=1,
            on_update=lambda v: self._on_enable_changed(v),
        )
        self.pvs["STATUS"] = builder.mbbIn(
            "STATUS",
            initial_value=0,
            ZRST="INIT",
            ONST="RUN",
            TWST="PAUSED",
            THST="END",
            FRST="ERROR",
        )
        self.pvs["MESSAGE"] = builder.stringIn("MESSAGE", initial_value="Initialized")

        if self.mode == "triggered":
            self.pvs["RUN"] = builder.boolOut(
                "RUN",
                initial_value=0,
                on_update=lambda v: self._on_run_trigger(v),
            )
        if self.mode == "continuous":
            self.pvs["CYCLE_COUNT"] = builder.longIn("CYCLE_COUNT", initial_value=0)

        reserved = {"STATUS", "MESSAGE", "ENABLE", "RUN", "CYCLE_COUNT"}
        for pv_name, pv_config in self.pv_definitions.get("inputs", {}).items():
            if pv_name in reserved:
                continue
            self.pvs[pv_name] = self._create_pv(pv_name, pv_config, is_output=True)

        for pv_name, pv_config in self.pv_definitions.get("outputs", {}).items():
            if pv_name in reserved:
                continue
            self.pvs[pv_name] = self._create_pv(pv_name, pv_config, is_output=False)

        self.logger.info(f"Created {len(self.pvs)} PVs with prefix: {self.pv_prefix}")

    @staticmethod
    def _create_pv(pv_name: str, config: Dict[str, Any], is_output: bool):
        from softioc import builder

        pv_type = config.get("type", "float")
        initial_value = config.get("value", 0)
        on_update = None

        if pv_type == "float":
            kwargs = dict(
                initial_value=float(initial_value),
                EGU=config.get("unit", ""),
                PREC=config.get("prec", 3),
                LOPR=config.get("low", 0),
                HOPR=config.get("high", 100),
            )
            if is_output:
                return builder.aOut(pv_name, on_update=on_update, **kwargs)
            return builder.aIn(pv_name, **kwargs)
        elif pv_type == "int":
            if is_output:
                return builder.longOut(pv_name, initial_value=int(initial_value), on_update=on_update)
            return builder.longIn(pv_name, initial_value=int(initial_value))
        elif pv_type == "string":
            if is_output:
                return builder.stringOut(pv_name, initial_value=str(initial_value), on_update=on_update)
            return builder.stringIn(pv_name, initial_value=str(initial_value))
        elif pv_type == "bool":
            kwargs = dict(
                initial_value=int(initial_value),
                ZNAM=config.get("znam", "Off"),
                ONAM=config.get("onam", "On"),
            )
            if is_output:
                return builder.boolOut(pv_name, on_update=on_update, **kwargs)
            return builder.boolIn(pv_name, **kwargs)
        else:
            if is_output:
                return builder.aOut(pv_name, initial_value=float(initial_value), on_update=on_update)
            return builder.aIn(pv_name, initial_value=float(initial_value))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the task in its own thread."""
        self.logger.info(f"Starting task: {self.name}")
        self.running = True
        self.set_status("RUN")
        self.set_message("Task running")

        if self.mode == "continuous":
            self._thread = threading.Thread(
                target=self._run_wrapper, name=f"task-{self.name}", daemon=True
            )
            self._thread.start()
        else:
            self.set_status("INIT")
            self.set_message("Ready for trigger")

    def _run_wrapper(self):
        try:
            while self.running and self.enabled:
                self.execute()
                self.step_cycle()
                interval = self.parameters.get("interval", 1.0)
                time.sleep(interval)
        except Exception as e:
            self.logger.error(f"Error in task execution: {e}", exc_info=True)
            self.set_status("ERROR")
            self.set_message(f"Error: {str(e)}")
            self.running = False

    def stop(self):
        """Stop the task gracefully."""
        self.logger.info(f"Stopping task: {self.name}")
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
        self.set_status("END")
        self.set_message("Task stopped")
        self.cleanup()

    # ------------------------------------------------------------------
    # PV helpers
    # ------------------------------------------------------------------

    def set_pv(self, pv_name: str, value: Any):
        if pv_name in self.pvs:
            self.pvs[pv_name].set(value)

    def get_pv(self, pv_name: str) -> Any:
        if pv_name in self.pvs:
            return self.pvs[pv_name].get()
        return None

    def set_status(self, status: str):
        status_map = {"INIT": 0, "RUN": 1, "PAUSED": 2, "END": 3, "ERROR": 4}
        if status.upper() in status_map and "STATUS" in self.pvs:
            try:
                self.pvs["STATUS"].set(status_map[status.upper()])
            except Exception:
                pass

    def set_message(self, message: str):
        if "MESSAGE" in self.pvs:
            try:
                self.pvs["MESSAGE"].set(str(message)[:39])
            except Exception:
                pass

    def _on_enable_changed(self, value):
        self.enabled = bool(value)
        if not self.enabled:
            self.set_status("PAUSED")
        else:
            self.set_status("RUN")

    # ------------------------------------------------------------------
    # Cycle helpers
    # ------------------------------------------------------------------

    def step_cycle(self):
        if self.mode != "continuous":
            return
        self.cycle_count += 1
        if "CYCLE_COUNT" in self.pvs:
            try:
                self.pvs["CYCLE_COUNT"].set(int(self.cycle_count))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Triggered mode
    # ------------------------------------------------------------------

    def _on_run_trigger(self, value: Any):
        if not bool(value):
            return
        try:
            self.pvs["RUN"].set(0)
        except Exception:
            pass
        with self.task_lock:
            if self._trigger_thread and self._trigger_thread.is_alive():
                return
            self._trigger_thread = threading.Thread(
                target=self._trigger_wrapper, name=f"{self.name}-trigger", daemon=True
            )
            self._trigger_thread.start()

    def _trigger_wrapper(self):
        self.set_status("RUN")
        self.set_message("Executing triggered action")
        try:
            self.triggered()
            self.set_status("END")
            self.set_message("Triggered run completed")
        except Exception as e:
            self.logger.error(f"Error in triggered run: {e}", exc_info=True)
            self.set_status("ERROR")
            self.set_message(f"Error: {str(e)}")
        finally:
            time.sleep(2)
            if self.enabled:
                self.set_status("INIT")
                self.set_message("Ready for trigger")

    def triggered(self):
        """Override to implement one-shot action for triggered mode."""
        pass

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    def get_datetime(self) -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def get_timems(self) -> int:
        return int(time.time() * 1000)

    def get_device(self, device_name: str):
        """Get an Ophyd device by name (lazy-created singleton if resolver is set)."""
        if self._device_resolver is not None:
            return self._device_resolver(device_name)
        return self.ophyd_devices.get(device_name)

    def list_devices(self):
        return list(self.ophyd_devices.keys())

    # ------------------------------------------------------------------
    # Abstract methods – subclasses MUST implement these
    # ------------------------------------------------------------------

    @abstractmethod
    def initialize(self):
        """Task-specific initialization. Called once before the run loop."""
        ...

    @abstractmethod
    def execute(self):
        """Called each cycle in continuous mode. Implement task logic here."""
        ...

    @abstractmethod
    def cleanup(self):
        """Task cleanup. Called when the task is stopped."""
        ...

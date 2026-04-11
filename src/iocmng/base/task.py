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

from iocmng.core.plugin_spec import PluginSpec, create_softioc_record


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
        plugin_prefix: Optional[str] = None,
        device_resolver: Optional[Any] = None,
        plugin_spec: Optional[PluginSpec] = None,
    ):
        """Initialize a task.

        Args:
            name: Unique task name.
            parameters: Task-specific parameters.
            pv_definitions: PV definitions (inputs/outputs) for soft IOC integration.
            beamline_config: Full beamline configuration dict.
            ophyd_devices: Dictionary of Ophyd device instances.
            prefix: Controller/beamline PV prefix.
            plugin_prefix: Optional task-specific PV prefix segment from config.yaml.
            device_resolver: Optional callable(name) -> device for lazy creation.
        """
        self.name = name
        self.plugin_spec = plugin_spec or PluginSpec.from_runtime(
            parameters=parameters,
            pv_definitions=pv_definitions,
            plugin_prefix=plugin_prefix or self.name.upper(),
        )
        self.parameters = dict(self.plugin_spec.parameters)
        self.pv_definitions = self.plugin_spec.pv_definitions
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
        self.plugin_prefix = self.plugin_spec.prefix or self.name.upper()
        self.pv_prefix = self._get_pv_prefix(prefix)
        self.logger.debug(
            "Task prefix resolution: name=%s controller_prefix=%r plugin_prefix=%r beamline=%r namespace=%r resolved_pv_prefix=%r",
            self.name,
            prefix,
            self.plugin_prefix,
            self.beamline_config.get("beamline"),
            self.beamline_config.get("namespace"),
            self.pv_prefix,
        )

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
            return f"{controller_prefix}:{self.plugin_prefix}"
        beamline = self.beamline_config.get("beamline", "BEAMLINE").upper()
        namespace = self.beamline_config.get("namespace", "DEFAULT").upper()
        if beamline == namespace:
            return f"{beamline}:{self.plugin_prefix}"
        return f"{beamline}:{namespace}:{self.plugin_prefix}"

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
        for pv_name, pv_spec in self.plugin_spec.inputs.items():
            if pv_name in reserved:
                continue
            self.pvs[pv_name] = create_softioc_record(pv_spec)

        for pv_name, pv_spec in self.plugin_spec.outputs.items():
            if pv_name in reserved:
                continue
            self.pvs[pv_name] = create_softioc_record(pv_spec)

        self.logger.info(f"Created {len(self.pvs)} PVs with prefix: {self.pv_prefix}")

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the task in its own thread."""
        self.logger.info(f"Starting task: {self.name}")
        # AS info dump for traceability at startup.
        self.logger.info(
            "AS_INFO task=%s mode=%s pv_prefix=%s parameters=%s pv_definitions=%s",
            self.name,
            self.mode,
            self.pv_prefix,
            self.parameters,
            self.pv_definitions,
        )
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

    def set_output(self, pv_name: str, value: Any):
        self.set_pv(pv_name, value)

    def get_output(self, pv_name: str) -> Any:
        return self.get_pv(pv_name)

    def set_input(self, pv_name: str, value: Any):
        self.set_pv(pv_name, value)

    def get_input(self, pv_name: str) -> Any:
        return self.get_pv(pv_name)

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

    def create_device(
        self,
        prefix: str,
        devgroup: str,
        devtype: str,
        name: Optional[str] = None,
        cache: bool = True,
    ):
        """Instantiate an Ophyd device by PV prefix, group and type.

        Uses the same ``DeviceFactory`` registry as the beamline controller so
        device classes, PV component suffixes and metadata are consistent.

        Supported ``(devgroup, devtype)`` pairs (non-exhaustive):

        +-----------+-------------------+------------------------------------------+
        | devgroup  | devtype           | Ophyd class                              |
        +===========+===================+==========================================+
        | ``mot``   | ``asyn``          | ``OphydAsynMotor`` (EPICS motor record)  |
        | ``mot``   | ``tml``           | ``OphydTmlMotor`` (TechnoSoft / TML)     |
        | ``mot``   | ``sim``           | ``OphydMotorSim``                        |
        | ``io``    | ``di``/``do``     | ``OphydDI`` / ``OphydDO``               |
        | ``io``    | ``ai``/``ao``     | ``OphydAI`` / ``OphydAO``               |
        | ``io``    | ``rtd``           | ``OphydRTD``                             |
        | ``mag``   | ``dante``         | ``OphydPSDante``                         |
        | ``mag``   | ``unimag``        | ``OphydPSUnimag``                        |
        | ``diag``  | ``bpm``           | ``SppOphydBpm``                          |
        | ``vac``   | ``ipcmini``       | ``OphydVPC``                             |
        +-----------+-------------------+------------------------------------------+

        Example — TML motor using SPARC:MOT:TML prefix::

            motor = self.create_device(
                prefix="SPARC:MOT:TML:GUNFLG01",
                devgroup="mot",
                devtype="tml",
                name="GUNFLG01",
            )
            pos = motor.user_readback.get()

        Example — standard asyn motor record::

            motor = self.create_device(
                prefix="SPARC:MOT:TML:GUNFLG01",
                devgroup="mot",
                devtype="asyn",
                name="GUNFLG01",
            )
            is_done = motor.motor_done_move.get()

        Args:
            prefix: Full PV prefix for the device (e.g. ``"SPARC:MOT:TML:GUNFLG01"``).
            devgroup: Device group key (``"mot"``, ``"io"``, ``"mag"``, ``"diag"``, ``"vac"``).
            devtype: Device type key (``"asyn"``, ``"tml"``, ``"di"``, ``"dante"`` …).
            name: Ophyd device ``name`` attribute.  Defaults to the last segment of *prefix*.
            cache: If *True* (default) the device is stored in ``self.ophyd_devices``
                under ``name`` and returned on subsequent calls without re-creating it.

        Returns:
            Ophyd device instance, or *None* if ophyd/infn_ophyd_hal are not available.
        """
        device_name = name or prefix.rsplit(":", 1)[-1]

        if cache and device_name in self.ophyd_devices:
            return self.ophyd_devices[device_name]

        try:
            from infn_ophyd_hal.device_factory import DeviceFactory
        except ImportError:
            self.logger.warning(
                "create_device: ophyd / infn_ophyd_hal not installed — returning None"
            )
            return None

        factory = DeviceFactory()
        device = factory.create_device(
            devgroup=devgroup,
            devtype=devtype,
            prefix=prefix,
            name=device_name,
        )
        if device is None:
            self.logger.warning(
                "create_device: DeviceFactory returned None for %s/%s prefix=%s",
                devgroup,
                devtype,
                prefix,
            )
            return None

        if cache:
            self.ophyd_devices[device_name] = device

        self.logger.debug(
            "create_device: %s/%s prefix=%s name=%s -> %s",
            devgroup,
            devtype,
            prefix,
            device_name,
            type(device).__name__,
        )
        return device

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

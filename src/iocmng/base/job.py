"""
Base class for one-shot jobs in the IOC Manager framework.

A job is a short-lived process that runs once and returns a result.
User applications should subclass JobBase and implement the required
abstract methods: initialize() and execute().
"""

import datetime
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class JobResult:
    """Result of a job execution."""

    def __init__(self, success: bool, data: Any = None, message: str = ""):
        self.success = success
        self.data = data
        self.message = message
        self.timestamp = datetime.datetime.now().isoformat()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "data": self.data,
            "message": self.message,
            "timestamp": self.timestamp,
        }


class JobBase(ABC):
    """Abstract base class for all IOC Manager jobs.

    A job runs once, produces a result, and terminates. Jobs are useful for
    one-shot operations like configuration deployment, diagnostics, or
    data collection snapshots.

    Subclasses must implement:
        - initialize(): one-time setup
        - execute(): the job logic, must return a JobResult

    Example::

        from iocmng import JobBase
        from iocmng.base.job import JobResult

        class MyDeployJob(JobBase):
            def initialize(self):
                self.logger.info("Preparing deployment")

            def execute(self) -> JobResult:
                # do deployment work
                return JobResult(success=True, message="Deployed OK")
    """

    # Class-level marker used for validation
    _iocmng_type = "job"

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
        """Initialize a job.

        Args:
            name: Unique job name.
            parameters: Job-specific parameters.
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
        self.logger = logging.getLogger(f"iocmng.job.{name}")
        self._last_result: Optional[JobResult] = None

        # PV storage
        self.pvs: Dict[str, Any] = {}

        # PV prefix
        self.pv_prefix = self._get_pv_prefix(prefix)

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

        Creates default control PVs (STATUS, MESSAGE) plus any PVs defined
        in pv_definitions from the plugin config YAML. Called by the controller
        before IOC initialization.
        """
        from softioc import builder

        builder.SetDeviceName(self.pv_prefix)

        self.pvs["STATUS"] = builder.mbbIn(
            "STATUS",
            initial_value=0,
            ZRST="IDLE",
            ONST="RUNNING",
            TWST="SUCCESS",
            THST="FAILED",
        )
        self.pvs["MESSAGE"] = builder.stringIn("MESSAGE", initial_value="Idle")

        reserved = {"STATUS", "MESSAGE"}
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

        if pv_type == "float":
            kwargs = dict(
                initial_value=float(initial_value),
                EGU=config.get("unit", ""),
                PREC=config.get("prec", 3),
                LOPR=config.get("low", 0),
                HOPR=config.get("high", 100),
            )
            if is_output:
                return builder.aOut(pv_name, **kwargs)
            return builder.aIn(pv_name, **kwargs)
        elif pv_type == "int":
            if is_output:
                return builder.longOut(pv_name, initial_value=int(initial_value))
            return builder.longIn(pv_name, initial_value=int(initial_value))
        elif pv_type == "string":
            if is_output:
                return builder.stringOut(pv_name, initial_value=str(initial_value))
            return builder.stringIn(pv_name, initial_value=str(initial_value))
        elif pv_type == "bool":
            kwargs = dict(
                initial_value=int(initial_value),
                ZNAM=config.get("znam", "Off"),
                ONAM=config.get("onam", "On"),
            )
            if is_output:
                return builder.boolOut(pv_name, **kwargs)
            return builder.boolIn(pv_name, **kwargs)
        else:
            if is_output:
                return builder.aOut(pv_name, initial_value=float(initial_value))
            return builder.aIn(pv_name, initial_value=float(initial_value))

    def set_pv(self, pv_name: str, value: Any):
        """Set a PV value by name."""
        pv = self.pvs.get(pv_name)
        if pv is not None:
            pv.set(value)

    def get_pv(self, pv_name: str) -> Any:
        """Get a PV value by name."""
        pv = self.pvs.get(pv_name)
        if pv is not None:
            return pv.get()
        return None

    def set_status(self, status: str):
        """Set the STATUS PV (IDLE=0, RUNNING=1, SUCCESS=2, FAILED=3)."""
        status_map = {"IDLE": 0, "RUNNING": 1, "SUCCESS": 2, "FAILED": 3}
        idx = status_map.get(status.upper(), 0)
        self.set_pv("STATUS", idx)

    def set_message(self, message: str):
        """Set the MESSAGE PV."""
        self.set_pv("MESSAGE", message[:39])

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def run(self) -> JobResult:
        """Execute the full job lifecycle: initialize -> execute -> return result."""
        self.logger.info(f"Running job: {self.name}")
        self.set_status("RUNNING")
        self.set_message("Running")
        try:
            self.initialize()
            result = self.execute()
            if not isinstance(result, JobResult):
                result = JobResult(success=True, data=result, message="Completed")
            self._last_result = result
            self.set_status("SUCCESS" if result.success else "FAILED")
            self.set_message(result.message[:39] if result.message else "Done")
            self.logger.info(f"Job {self.name} completed: {result.message}")
            return result
        except Exception as e:
            self.logger.error(f"Job {self.name} failed: {e}", exc_info=True)
            result = JobResult(success=False, message=str(e))
            self._last_result = result
            self.set_status("FAILED")
            self.set_message(str(e)[:39])
            return result

    @property
    def last_result(self) -> Optional[JobResult]:
        return self._last_result

    def get_device(self, device_name: str):
        """Get an Ophyd device by name (lazy-created singleton if resolver is set)."""
        if self._device_resolver is not None:
            return self._device_resolver(device_name)
        return self.ophyd_devices.get(device_name)

    def list_devices(self):
        return list(self.ophyd_devices.keys())

    def get_datetime(self) -> str:
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    def get_timems(self) -> int:
        return int(time.time() * 1000)

    @abstractmethod
    def initialize(self):
        """Job-specific initialization. Called once before execute()."""
        ...

    @abstractmethod
    def execute(self) -> JobResult:
        """Execute the job logic. Must return a JobResult."""
        ...

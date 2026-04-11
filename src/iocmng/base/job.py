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

from iocmng.core.plugin_spec import PluginSpec, create_softioc_record


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
        plugin_prefix: Optional[str] = None,
        device_resolver: Optional[Any] = None,
        plugin_spec: Optional[PluginSpec] = None,
    ):
        """Initialize a job.

        Args:
            name: Unique job name.
            parameters: Job-specific parameters.
            pv_definitions: PV definitions (inputs/outputs) for soft IOC integration.
            beamline_config: Full beamline configuration dict.
            ophyd_devices: Dictionary of Ophyd device instances.
            prefix: Controller/beamline PV prefix.
            plugin_prefix: Optional job-specific PV prefix segment from config.yaml.
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
        self.logger = logging.getLogger(f"iocmng.job.{name}")
        self._last_result: Optional[JobResult] = None

        # PV storage
        self.pvs: Dict[str, Any] = {}

        # PV prefix
        self.plugin_prefix = self.plugin_spec.prefix or self.name.upper()
        self.pv_prefix = self._get_pv_prefix(prefix)
        self.logger.debug(
            "Job prefix resolution: name=%s controller_prefix=%r plugin_prefix=%r beamline=%r namespace=%r resolved_pv_prefix=%r",
            self.name,
            prefix,
            self.plugin_prefix,
            self.beamline_config.get("beamline"),
            self.beamline_config.get("namespace"),
            self.pv_prefix,
        )

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
        for pv_name, pv_spec in self.plugin_spec.inputs.items():
            if pv_name in reserved:
                continue
            self.pvs[pv_name] = create_softioc_record(pv_spec)

        for pv_name, pv_spec in self.plugin_spec.outputs.items():
            if pv_name in reserved:
                continue
            self.pvs[pv_name] = create_softioc_record(pv_spec)

        self.logger.info(f"Created {len(self.pvs)} PVs with prefix: {self.pv_prefix}")

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

    def set_output(self, pv_name: str, value: Any):
        self.set_pv(pv_name, value)

    def get_output(self, pv_name: str) -> Any:
        return self.get_pv(pv_name)

    def set_input(self, pv_name: str, value: Any):
        self.set_pv(pv_name, value)

    def get_input(self, pv_name: str) -> Any:
        return self.get_pv(pv_name)

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

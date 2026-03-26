#!/usr/bin/env python3
"""
Base class for all beamline tasks with soft IOC integration using softioc library.
"""

import datetime
import logging
import threading
import time
from abc import ABC, abstractmethod
from typing import Dict, Any, List
from softioc import softioc, builder
import cothread


class TaskBase(ABC):
    """Abstract base class for all beamline tasks."""

    def __init__(
        self,
        name: str,
        parameters: Dict[str, Any],
        pv_definitions: Dict[str, Any],
        beamline_config: Dict[str, Any],
        ophyd_devices: Dict[str, object] = None,
        prefix: str = None,
    ):
        """
        Initialize task.

        Args:
            name: Task name
            parameters: Task-specific parameters from config
            pv_definitions: PV definitions (inputs/outputs)
            beamline_config: Full beamline configuration from values.yaml
            ophyd_devices: Dictionary of Ophyd device instances from beamline
            prefix: PV prefix from main controller (overrides beamline_config)
        """
        self.name = name
        self.parameters = parameters
        self.pv_definitions = pv_definitions
        self.beamline_config = beamline_config
        self.ophyd_devices = ophyd_devices or {}

        self.logger = logging.getLogger(name)

        # PV storage - will hold references to builder PV objects
        self.pvs = {}

        # Task control
        self.enabled = True
        self.running = False
        self.task_lock = threading.Lock()

        # Get PV prefix from controller or beamline config
        self.pv_prefix = self._get_pv_prefix(prefix)
        self.logger.debug(
            "Legacy task prefix resolution: name=%s controller_prefix=%r beamline=%r namespace=%r resolved_pv_prefix=%r",
            self.name,
            prefix,
            self.beamline_config.get("beamline"),
            self.beamline_config.get("namespace"),
            self.pv_prefix,
        )

        # Task mode: 'continuous' (default) or 'triggered'
        mode = parameters.get("mode") or (
            "triggered" if parameters.get("triggered") else "continuous"
        )
        self.mode = str(mode).lower() if isinstance(mode, str) else "continuous"
        if self.mode not in ("continuous", "triggered"):
            self.mode = "continuous"

        # Cycle counter for continuous tasks
        self.cycle_count = 0
        # For triggered execution
        self._trigger_thread = None

    def _get_pv_prefix(self, controller_prefix: str = None) -> str:
        """
        Get PV prefix for this task.

        Args:
            controller_prefix: Prefix provided by main controller

        Returns:
            PV prefix string
        """
        # Use controller prefix if provided, otherwise extract from beamline config
        if controller_prefix:
            return f"{controller_prefix}:{self.name.upper()}"

        # Fallback to extracting from beamline config
        beamline = self.beamline_config.get("beamline", "BEAMLINE").upper()
        namespace = self.beamline_config.get("namespace", "DEFAULT").upper()
        if beamline == namespace:
            return f"{beamline}:{self.name.upper()}"
        return f"{beamline}:{namespace}:{self.name.upper()}"

    def _create_pvs(self):
        """Create PVs using softioc builder."""
        # Set the device name (prefix)
        builder.SetDeviceName(self.pv_prefix)

        # Create enable/disable control PV
        self.pvs["ENABLE"] = builder.boolOut(
            "ENABLE",
            initial_value=1,
            on_update=lambda value: self._on_enable_changed(value),
        )

        # Create STATUS multistate PV (INIT=0, RUN=1, PAUSED=2, END=3, ERROR=4)
        self.pvs["STATUS"] = builder.mbbIn(
            "STATUS",
            initial_value=0,
            ZRST="INIT",
            ONST="RUN",
            TWST="PAUSED",
            THST="END",
            FRST="ERROR",
        )

        # Create MESSAGE string PV for task messages and errors
        self.pvs["MESSAGE"] = builder.stringIn("MESSAGE", initial_value="Initialized")

        # Create RUN trigger button for triggered tasks
        if self.mode == "triggered":
            self.pvs["RUN"] = builder.boolOut(
                "RUN",
                initial_value=0,
                on_update=lambda value: self._on_run_trigger(value),
            )

        # Cycle counter for continuous tasks
        if self.mode == "continuous":
            self.pvs["CYCLE_COUNT"] = builder.longIn("CYCLE_COUNT", initial_value=0)

        # Create input PVs (skip reserved names: STATUS, MESSAGE, ENABLE, RUN, CYCLE_COUNT)
        reserved_names = {"STATUS", "MESSAGE", "ENABLE", "RUN", "CYCLE_COUNT"}
        for pv_name, pv_config in self.pv_definitions.get("inputs", {}).items():
            if pv_name in reserved_names:
                self.logger.warning(f"Skipping reserved PV name in inputs: {pv_name}")
                continue
            pv_obj = self._create_pv(
                pv_name, pv_config, is_output=True
            )  # Input to IOC is Out
            self.pvs[pv_name] = pv_obj

        # Create output PVs (skip reserved names)
        for pv_name, pv_config in self.pv_definitions.get("outputs", {}).items():
            if pv_name in reserved_names:
                self.logger.warning(f"Skipping reserved PV name in outputs: {pv_name}")
                continue
            pv_obj = self._create_pv(
                pv_name, pv_config, is_output=False
            )  # Output from IOC is In
            self.pvs[pv_name] = pv_obj

        self.logger.info(f"Created {len(self.pvs)} PVs with prefix: {self.pv_prefix}")

    def build_pvs(self):
        """Public method to build PVs without initializing the IOC.

        Use this when coordinating multiple tasks so that all records are created
        before a single global LoadDatabase/iocInit is performed.
        """
        self._create_pvs()

    def _create_pv(self, pv_name: str, config: Dict[str, Any], is_output: bool):
        """
        Create a single PV using softioc builder.

        Args:
            pv_name: Name of the PV
            config: PV configuration dictionary
            is_output: True if PV is an output (writable), False if input (readable)

        Returns:
            PV object from builder
        """
        pv_type = config.get("type", "float")
        initial_value = config.get("value", 0)

        # Create callback if it's an output (writable) PV
        on_update = None
        if is_output:
            on_update = lambda value, name=pv_name: self.on_pv_write(name, value)

        # Create PV based on type
        if pv_type == "float":
            if is_output:
                pv = builder.aOut(
                    pv_name,
                    initial_value=float(initial_value),
                    on_update=on_update,
                    EGU=config.get("unit", ""),
                    PREC=config.get("prec", 3),
                    LOPR=config.get("low", 0),
                    HOPR=config.get("high", 100),
                )
            else:
                pv = builder.aIn(
                    pv_name,
                    initial_value=float(initial_value),
                    EGU=config.get("unit", ""),
                    PREC=config.get("prec", 3),
                    LOPR=config.get("low", 0),
                    HOPR=config.get("high", 100),
                )

        elif pv_type == "int":
            if is_output:
                pv = builder.longOut(
                    pv_name, initial_value=int(initial_value), on_update=on_update
                )
            else:
                pv = builder.longIn(pv_name, initial_value=int(initial_value))

        elif pv_type == "string":
            if is_output:
                pv = builder.stringOut(
                    pv_name, initial_value=str(initial_value), on_update=on_update
                )
            else:
                pv = builder.stringIn(pv_name, initial_value=str(initial_value))

        elif pv_type == "bool":
            if is_output:
                pv = builder.boolOut(
                    pv_name,
                    initial_value=int(initial_value),
                    on_update=on_update,
                    ZNAM=config.get("znam", "Off"),
                    ONAM=config.get("onam", "On"),
                )
            else:
                pv = builder.boolIn(
                    pv_name,
                    initial_value=int(initial_value),
                    ZNAM=config.get("znam", "Off"),
                    ONAM=config.get("onam", "On"),
                )
        else:
            # Default to float
            if is_output:
                pv = builder.aOut(
                    pv_name, initial_value=float(initial_value), on_update=on_update
                )
            else:
                pv = builder.aIn(pv_name, initial_value=float(initial_value))

        return pv

    def _on_enable_changed(self, value):
        """Handle enable/disable PV changes."""
        self.enabled = bool(value)
        status_msg = "enabled" if self.enabled else "disabled"
        self.logger.info(f"Task {status_msg}")
        self.set_message(f"Task {status_msg}")
        if not self.enabled:
            self.set_status("PAUSED")
        else:
            self.set_status("RUN")
        self.on_pv_write("ENABLE", value)

    def start(self):
        """Start the task and its IOC."""
        self.logger.info(f"Starting task: {self.name}")

        # Create PVs
        self._create_pvs()

        # Load database and initialize IOC
        builder.LoadDatabase()
        softioc.iocInit()

        self.logger.info(f"IOC initialized for task: {self.name}")

        # Call task-specific initialization
        # self.initialize()

        # Set running flag and status
        self.running = True
        self.set_status("RUN")
        self.set_message("Task running")

        # Start task execution loop only for continuous tasks
        if self.mode == "continuous":
            cothread.Spawn(self._run_wrapper)
        else:
            self.logger.info(
                "Triggered mode: no continuous run loop started. Use RUN to trigger execution."
            )
            self.set_status("INIT")
            self.set_message("Ready for trigger")

    def start_after_ioc(self):
        """Start the task assuming IOC is already initialized globally."""
        self.logger.info(f"Starting task (post-IOC): {self.name}")

        # Task-specific initialization
       # self.initialize()

        # Set running flag and status
        self.running = True
        self.set_status("RUN")
        self.set_message("Task running")

        # Start task execution loop only for continuous tasks
        if self.mode == "continuous":
            cothread.Spawn(self._run_wrapper)
        else:
            self.logger.info(
                "Triggered mode: no continuous run loop started. Use RUN to trigger execution."
            )
            self.set_status("INIT")
            self.set_message("Ready for trigger")

    def _run_wrapper(self):
        """Wrapper for run method to handle exceptions."""
        try:
            self.run()
        except Exception as e:
            self.logger.error(f"Error in task execution: {e}", exc_info=True)
            self.set_status("ERROR")
            self.set_message(f"Error: {str(e)}")
            self.running = False

    def stop(self):
        """Stop the task and its IOC."""
        self.logger.info(f"Stopping task: {self.name}")

        # Set running flag to false
        self.running = False
        self.set_status("END")
        self.set_message("Task stopped")

        # Call task-specific cleanup
        self.cleanup()

        self.logger.info(f"Task stopped: {self.name}")

    def on_pv_write(self, pv_name: str, value: Any):
        """
        Callback when a PV is written.

        Args:
            pv_name: Name of the PV that was written
            value: New value
        """
        # Call task-specific handler
        self.handle_pv_write(pv_name, value)

    def set_pv(self, pv_name: str, value: Any):
        """
        Set a PV value from within the task.

        Args:
            pv_name: Name of the PV (without prefix)
            value: Value to set
        """
        if pv_name in self.pvs:
            self.pvs[pv_name].set(value)
        else:
            self.logger.warning(f"PV {pv_name} not found")

    def get_pv(self, pv_name: str) -> Any:
        """
        Get a PV value.

        Args:
            pv_name: Name of the PV (without prefix)

        Returns:
            Current value of the PV
        """
        if pv_name in self.pvs:
            return self.pvs[pv_name].get()
        else:
            self.logger.warning(f"PV {pv_name} not found")
            return None

    def set_status(self, status: str):
        """
        Set the task STATUS PV.

        Args:
            status: One of 'INIT', 'RUN', 'PAUSED', 'END', 'ERROR'
        """
        status_map = {"INIT": 0, "RUN": 1, "PAUSED": 2, "END": 3, "ERROR": 4}
        status_upper = status.upper()
        if status_upper in status_map and "STATUS" in self.pvs:
            try:
                self.pvs["STATUS"].set(status_map[status_upper])
            except Exception as e:
                self.logger.warning(f"Failed to set STATUS: {e}")

    def set_message(self, message: str):
        """
        Set the task MESSAGE PV.

        Args:
            message: Message string to display
        """
        if "MESSAGE" in self.pvs:
            try:
                self.pvs["MESSAGE"].set(str(message)[:39])  # EPICS string limit
            except Exception as e:
                self.logger.warning(f"Failed to set MESSAGE: {e}")

    def get_device(self, device_name: str):
        """
        Get an Ophyd device by name.

        Args:
            device_name: Name of the device (IOC name or IOC_DEVICE format)

        Returns:
            Ophyd device instance or None if not found
        """
        if device_name in self.ophyd_devices:
            return self.ophyd_devices[device_name]
        else:
            self.logger.warning(f"Ophyd device {device_name} not found")
            return None

    def list_devices(self) -> List[str]:
        """
        Get list of available Ophyd device names.

        Returns:
            List of device names
        """
        return list(self.ophyd_devices.keys())

    def get_devices_by_type(self, devgroup: str) -> Dict[str, object]:
        """
        Get all Ophyd devices of a specific type.

        Args:
            devgroup: Device group (e.g., 'motor', 'diag', 'vac')

        Returns:
            Dictionary of matching devices
        """
        # This is a simple implementation - could be enhanced with device metadata
        matching_devices = {}
        for name, device in self.ophyd_devices.items():
            # You could check device type/class here
            # For now, return all and let the task filter
            matching_devices[name] = device
        return matching_devices

    @abstractmethod
    def initialize(self):
        """Task-specific initialization. Must be implemented by subclasses."""
        pass

    def run(self):
        """Default main execution loop for continuous tasks.

        Subclasses should override for custom behavior. Triggered tasks typically
        do not need a continuous run loop.
        """
        self.logger.info(
            "Default run loop started (no-op). Override run() for custom behavior."
        )
        while self.running and self.mode == "continuous":
            cothread.Sleep(0.5)

    @abstractmethod
    def cleanup(self):
        """Task cleanup. Must be implemented by subclasses."""
        pass

    def handle_pv_write(self, pv_name: str, value: Any):
        """
        Task-specific PV write handler. Can be overridden by subclasses.

        Args:
            pv_name: Name of the PV that was written
            value: New value
        """
        pass

    ## get_datetime
    def get_datetime(self) -> str:
        """Get current date and time as string with millisecond precision."""
        return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

    ## get_timems
    def get_timems(self) -> int:
        """Get current time in milliseconds since epoch."""
        return int(time.time() * 1000)

    # --------------------
    # Continuous helpers
    # --------------------
    def step_cycle(self):
        """Increment cycle counter for continuous tasks and update PV."""
        if self.mode != "continuous":
            return
        self.cycle_count += 1
        if "CYCLE_COUNT" in self.pvs:
            try:
                self.pvs["CYCLE_COUNT"].set(int(self.cycle_count))
            except Exception:
                pass

    ## get_cycle_count
    def get_cycle(self) -> int:
        """Get current cycle count."""
        return self.cycle_count

    # --------------------
    # Triggered helpers
    # --------------------
    def _on_run_trigger(self, value: Any):
        """Handle RUN button for triggered tasks."""
        try:
            pressed = bool(value)
        except Exception:
            pressed = False
        if not pressed:
            return
        # Reset the button
        try:
            self.pvs["RUN"].set(0)
        except Exception:
            pass

        # Launch one-shot execution if not already running a trigger
        with self.task_lock:
            if self._trigger_thread and self._trigger_thread.is_alive():
                self.logger.warning("Trigger ignored: previous run still in progress")
                return
            self._trigger_thread = threading.Thread(
                target=self._trigger_wrapper, name=f"{self.name}-trigger"
            )
            self._trigger_thread.daemon = True
            self._trigger_thread.start()

    def _trigger_wrapper(self):
        """Wrapper for triggered execution with error handling."""
        self.logger.info("Triggered run started")
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
            self.logger.info("Triggered run finished")
            # Return to INIT state for next trigger
            cothread.Sleep(2)  # Brief delay to show completion
            if self.enabled:
                self.set_status("INIT")
                self.set_message("Ready for trigger")

    def triggered(self):
        """Override in subclasses to implement the one-shot action for triggered mode."""
        self.logger.info("No triggered action implemented for this task")

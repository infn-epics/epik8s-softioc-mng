#!/usr/bin/env python3
"""
Modular Beamline Controller Application
Manages multiple tasks in dedicated threads with soft IOC integration using softioc library.
Creates Ophyd device instances for each IOC/device defined in values.yaml.
"""

import argparse
import logging
import sys
import os
import subprocess
import shutil
from pathlib import Path
from typing import Dict, List
import yaml
from importlib import import_module
import cothread

from task_base import TaskBase
from softioc import softioc, builder
from infn_ophyd_hal.device_factory import DeviceFactory

__version__ = "1.0.0"


class BeamlineController:
    """Main controller for beamline tasks."""

    def __init__(
        self,
        config_path: str,
        values_path: str,
        pvout_path: str = "pvlist.txt",
        disable_ophyd: bool = False,
    ):
        """
        Initialize the beamline controller.

        Args:
            config_path: Path to config.yaml
            values_path: Path to values.yaml (beamline configuration)
            pvout_path: Path to output PV list file
            disable_ophyd: If True, skip Ophyd device creation
        """
        self.logger = logging.getLogger(__name__)
        self.config_path = config_path
        self.values_path = values_path
        self.pvout_path = pvout_path
        self.disable_ophyd = disable_ophyd

        # Load configurations
        self.config = self._load_yaml(config_path)
        self.beamline_values = self._load_yaml(values_path)
        prefix_override = os.environ.get("IOCMNG_PREFIX", "").strip()
        if prefix_override:
            self.config["prefix"] = prefix_override

        # Task management
        self.tasks: List[TaskBase] = []
        self.prefix = self.config.get("prefix", "BEAMLINE:CONTROL")
        # Ophyd device management
        self.ophyd_devices: Dict[str, object] = {}
        self.ophyd_factory = DeviceFactory()
        self.logger.info(f"BeamlineController {__version__} initialized")
        self.logger.debug(
            "BeamlineController config loaded: config_path=%s values_path=%s config_prefix=%r beamline=%r namespace=%r",
            self.config_path,
            self.values_path,
            self.prefix,
            self.beamline_values.get("beamline"),
            self.beamline_values.get("namespace"),
        )
        if prefix_override:
            self.logger.info("Using IOCMNG_PREFIX override for controller prefix: %s", self.prefix)

    def _load_yaml(self, path: str) -> Dict:
        """Load YAML configuration file."""
        try:
            with open(path, "r") as f:
                return yaml.safe_load(f)
        except Exception as e:
            self.logger.error(f"Failed to load {path}: {e}")
            raise

    def _ensure_tasks_directory(self):
        """Ensure tasks directory exists, clone from git if necessary."""
        tasks_dir = Path(__file__).parent / "tasks"

        if tasks_dir.exists() and tasks_dir.is_dir():
            self.logger.info(f"Tasks directory found at {tasks_dir}")
            return

        # Check if git repo config exists
        tasksrepo = self.config.get("tasksrepo")
        tasksrev = self.config.get("tasksrev", "main")

        if not tasksrepo:
            self.logger.error(
                "Tasks directory not found and no 'tasksrepo' configured in config.yaml"
            )
            raise FileNotFoundError(
                "Tasks directory not found and no git repository configured"
            )

        self.logger.info(
            f"Tasks directory not found. Cloning from {tasksrepo} (branch/tag: {tasksrev})..."
        )

        try:
            # Create a temporary directory for cloning
            temp_dir = Path(__file__).parent / "temp_tasks_clone"

            # Remove temp directory if it exists
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

            # Clone the repository
            cmd = [
                "git",
                "clone",
                "--depth",
                "1",
                "-b",
                tasksrev,
                tasksrepo,
                str(temp_dir),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            self.logger.debug(f"Git clone output: {result.stdout}")

            # Check if tasks directory exists in the cloned repo
            cloned_tasks = temp_dir / "tasks"
            if cloned_tasks.exists() and cloned_tasks.is_dir():
                # Move tasks directory to the correct location
                shutil.move(str(cloned_tasks), str(tasks_dir))
                self.logger.info(f"Tasks directory successfully cloned to {tasks_dir}")
            else:
                # If no tasks subdirectory, use the entire cloned repo as tasks
                shutil.move(str(temp_dir), str(tasks_dir))
                self.logger.info(f"Repository cloned as tasks directory to {tasks_dir}")

            # Clean up temp directory if it still exists
            if temp_dir.exists():
                shutil.rmtree(temp_dir)

        except subprocess.CalledProcessError as e:
            self.logger.error(f"Failed to clone tasks repository: {e.stderr}")
            raise
        except Exception as e:
            self.logger.error(f"Error setting up tasks directory: {e}", exc_info=True)
            raise

    def _load_task_class(self, task_name: str):
        """
        Dynamically load task class from current directory or tasks module.

        Args:
            task_name: Name of the task module (e.g., 'example_task')

        Returns:
            Task class
        """
        import sys
        import os
        from pathlib import Path

        # Get class with same name as module (CamelCase)
        class_name = "".join(word.capitalize() for word in task_name.split("_"))

        # Get the current directory (where main.py is located)
        current_dir = Path(__file__).parent
        task_file = current_dir / f"{task_name}.py"

        # First try to load from current directory
        if task_file.exists():
            try:
                # Add current directory to sys.path temporarily
                if str(current_dir) not in sys.path:
                    sys.path.insert(0, str(current_dir))
                module = import_module(task_name)
                task_class = getattr(module, class_name)
                self.logger.debug(f"Loaded task {task_name} from current directory")
                return task_class
            except (ImportError, AttributeError) as e:
                self.logger.debug(
                    f"Failed to load {task_name} from current directory: {e}"
                )
                # Remove from sys.path if we added it
                if str(current_dir) in sys.path:
                    sys.path.remove(str(current_dir))
        else:
            self.logger.debug(f"Task file {task_file} not found in current directory")

        # Try to load from tasks directory
        try:
            module = import_module(f"tasks.{task_name}")
            task_class = getattr(module, class_name)
            self.logger.debug(f"Loaded task {task_name} from tasks directory")
            return task_class
        except (ImportError, AttributeError) as e:
            self.logger.error(
                f"Failed to load task {task_name}: not found in current directory or tasks/ subdirectory"
            )
            raise ImportError(
                f"Task module '{task_name}' not found. Expected class '{class_name}' in module."
            ) from e

    def initialize_ophyd_devices(self):
        """Initialize Ophyd devices from IOC configuration in values.yaml."""
        self.logger.info("Initializing Ophyd devices from beamline configuration...")

        # Get IOC configurations from values.yaml
        epics_config = self.beamline_values.get("epicsConfiguration", {})
        iocs = epics_config.get("iocs", [])

        for ioc_config in iocs:
            ioc_name = ioc_config.get("name")
            if not ioc_name:
                continue

            # Check if IOC is disabled
            if ioc_config.get("disable", False):
                self.logger.debug(f"Skipping disabled IOC: {ioc_name}")
                continue

            # Get device group to determine Ophyd class
            devgroup = ioc_config.get("devgroup")
            devtype = ioc_config.get("devtype")

            if not devgroup:
                self.logger.debug(
                    f"IOC {ioc_name} has no devgroup, skipping Ophyd creation"
                )
                continue

            # Get IOC prefix for PV construction
            ioc_prefix = ioc_config.get("iocprefix", "")

            beamline = self.beamline_values.get("beamline", "BEAMLINE").upper()
            namespace = self.beamline_values.get("namespace", "DEFAULT").upper()

            # Get devices list (for IOCs with multiple devices)
            devices = ioc_config.get("devices", [])
            iocname = ioc_config.get("name", "")
            try:
                if devices:
                    # Create Ophyd instance for each device
                    for device_config in devices:
                        device_name = device_config.get("name")
                        if not device_name:
                            continue
                        if "iocroot" in ioc_config:
                            pv_prefix = (
                                f"{ioc_prefix}:{ioc_config['iocroot']}:{device_name}"
                            )
                        else:
                            pv_prefix = f"{ioc_prefix}:{device_name}"

                        # Construct full PV prefix
                        myconfig = ioc_config.copy()
                        myconfig["iocname"] = iocname
                        myconfig.update(device_config)
                        # Create Ophyd device
                        ophyd_device = self.ophyd_factory.create_device(
                            devgroup=devgroup,
                            devtype=devtype,
                            prefix=pv_prefix,
                            name=device_name,
                            config=myconfig,
                        )

                        if ophyd_device:
                            device_key = f"{device_name}"
                            ## check if device_key already exists
                            if device_key in self.ophyd_devices:
                                d = f"{ioc_name}_{device_name}"
                                self.logger.warning(
                                    f"Device key '{device_key}' already exists, renaming to avoid conflict: {d}"
                                )
                                if d in self.ophyd_devices:
                                    self.logger.error(
                                        f"Renamed device key '{d}' also exists. Skipping device creation for {device_name} in IOC {ioc_name}."
                                    )
                                    continue
                                device_key = d

                            self.ophyd_devices[device_key] = ophyd_device
                            self.logger.info(
                                f"Created Ophyd device: {device_key} ({ioc_name}/{devgroup}/{devtype} prefix={pv_prefix})"
                            )
                else:
                    # Single device IOC
                    pv_prefix = f"{ioc_prefix}"

                    # Create Ophyd device
                    ophyd_device = self.ophyd_factory.create_device(
                        devgroup=devgroup,
                        devtype=devtype,
                        prefix=pv_prefix,
                        name=ioc_name,
                        config=ioc_config,
                    )

                    if ophyd_device:
                        self.ophyd_devices[ioc_name] = ophyd_device
                        self.logger.info(
                            f"Created Ophyd device: {ioc_name} ({devgroup}/{devtype})"
                        )

            except Exception as e:
                self.logger.error(
                    f"Failed to create Ophyd device for {ioc_name}: {e}", exc_info=True
                )

        self.logger.info(f"Created {len(self.ophyd_devices)} Ophyd devices")

    def initialize_tasks(self):
        """Initialize all tasks from configuration."""
        self.logger.info("Initializing tasks...")

        task_configs = self.config.get("tasks", [])
        # from iocmng_task import IocMngTask

        # # Always include IOC Manager task
        # ioc_manager_task = IocMngTask(
        #     name="ioc_manager",
        #     parameters={},
        #     pv_definitions={},
        #     beamline_config=self.beamline_values,
        #     ophyd_devices=self.ophyd_devices,
        #     prefix=self.prefix
        # )
        # self.tasks.append(ioc_manager_task)

        for task_config in task_configs:
            task_name = task_config.get("name")
            task_module = task_config.get("module")

            if not task_name or not task_module:
                self.logger.warning(
                    f"Skipping invalid task configuration: {task_config}"
                )
                continue

            try:
                # Load task class
                TaskClass = self._load_task_class(task_module)

                # Get task-specific parameters
                parameters = task_config.get("parameters", {})

                # Get PV definitions for this task
                pv_definitions = task_config.get("pvs", {})

                # Create task instance
                self.logger.debug(
                    "Creating legacy task instance: name=%s controller_prefix=%r beamline=%r namespace=%r parameters=%s",
                    task_name,
                    self.prefix,
                    self.beamline_values.get("beamline"),
                    self.beamline_values.get("namespace"),
                    sorted(parameters.keys()),
                )
                task = TaskClass(
                    name=task_name,
                    parameters=parameters,
                    pv_definitions=pv_definitions,
                    beamline_config=self.beamline_values,
                    ophyd_devices=self.ophyd_devices,
                    prefix=self.prefix,
                )

                self.tasks.append(task)
                self.logger.info(f"Initialized task: {task_name}")

            except Exception as e:
                self.logger.error(
                    f"Failed to initialize task {task_name}: {e}", exc_info=True
                )

    def start_tasks(self):
        """Start all tasks with a single IOC initialization.

        Steps:
        1) Build all PVs for every task
        2) Load the database and initialize the IOC ONCE
        3) Start each task's logic after IOC is up
        """
        self.logger.info("Building task PVs...")

        # 1) Build PVs for all tasks first
        for task in self.tasks:
            try:
                task.build_pvs()
                self.logger.info(f"Built PVs for task: {task.name}")
            except Exception as e:
                self.logger.error(
                    f"Failed to build PVs for task {task.name}: {e}", exc_info=True
                )

        for task in self.tasks:
            try:
                task.initialize()
                self.logger.info(f"Initialize task: {task.name}")
            except Exception as e:
                self.logger.error(
                    f"Failed to Initialize task {task.name}: {e}", exc_info=True
                )
        # 2) Initialize IOC once
        self.logger.info("Loading database and initializing IOC once for all tasks...")
        try:
            builder.LoadDatabase()
            softioc.iocInit()
            self.logger.info("IOC initialized")
            softioc.dbl()

            # Write PV list to file
            self.logger.info(f"Writing PV list to {self.pvout_path}")
            with open(self.pvout_path, "w") as f:
                old_stdout = os.dup(1)
                os.dup2(f.fileno(), 1)
                softioc.dbl()
                os.dup2(old_stdout, 1)
                os.close(old_stdout)
        except Exception as e:
            self.logger.error(f"IOC initialization failed: {e}", exc_info=True)
            raise

        # 3) Start each task's processing after IOC is ready
        self.logger.info("Starting task logic...")
        for task in self.tasks:
            try:
                task.start_after_ioc()
                self.logger.info(f"Started task: {task.name}")
            except Exception as e:
                self.logger.error(
                    f"Failed to start task {task.name}: {e}", exc_info=True
                )

    def stop_tasks(self):
        """Stop all tasks gracefully."""
        self.logger.info("Stopping tasks...")

        for task in self.tasks:
            try:
                task.stop()
            except Exception as e:
                self.logger.error(
                    f"Error stopping task {task.name}: {e}", exc_info=True
                )

        self.logger.info("All tasks stopped")

    def run(self):
        """Main run loop."""
        try:
            # Ensure tasks directory exists before initializing
            self._ensure_tasks_directory()

            if not self.disable_ophyd:
                self.initialize_ophyd_devices()
            else:
                self.logger.info("Ophyd device creation disabled")

            self.initialize_tasks()
            self.start_tasks()

            self.logger.info("Beamline Controller running. Press Ctrl+C to stop.")

            # Run the cothread dispatcher
            cothread.WaitForQuit()

        except KeyboardInterrupt:
            self.logger.info("Received shutdown signal")
        except Exception as e:
            self.logger.error(f"Fatal error: {e}", exc_info=True)
        finally:
            self.stop_tasks()


def setup_logging(level: str = "INFO"):
    """Configure logging."""
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Beamline Controller Application")
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--config", type=str, default="config.yaml", help="Path to config.yaml"
    )
    parser.add_argument(
        "--beamline",
        type=str,
        default="values.yaml",
        help="Path to yaml beamline configuration",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level",
    )
    parser.add_argument(
        "--pvout", type=str, default="pvlist.txt", help="Output PV list file"
    )
    parser.add_argument(
        "--disable-ophyd", action="store_true", help="Disable Ophyd device creation"
    )

    args = parser.parse_args()

    # Setup logging
    setup_logging(args.log_level)

    # Create and run controller
    controller = BeamlineController(
        args.config, args.beamline, args.pvout, args.disable_ophyd
    )
    controller.run()


if __name__ == "__main__":
    main()

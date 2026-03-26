"""
Central controller that manages loaded tasks and jobs at runtime.
"""

import logging
import threading
import os
import sys
from pathlib import Path
import yaml
from typing import Any, Dict, List, Optional, Tuple

from iocmng.base.task import TaskBase
from iocmng.base.job import JobBase, JobResult
from iocmng.core.loader import PluginLoader
from iocmng.core.validator import ValidationResult

logger = logging.getLogger(__name__)


def _deep_merge(base: Dict, override: Dict) -> Dict:
    """Recursively merge *override* into *base*, returning a new dict."""
    result = dict(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class PluginInfo:
    """Metadata about a loaded plugin."""

    def __init__(
        self,
        name: str,
        git_url: str,
        plugin_type: str,
        class_name: str,
        path: str = "",
        branch: str = "main",
        pat: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        start_parameters: Optional[Dict[str, Any]] = None,
        pv_definitions: Optional[Dict[str, Any]] = None,
        plugin_prefix: Optional[str] = None,
        auto_start: bool = False,
        auto_start_on_boot: bool = False,
        autostart_order: Optional[int] = None,
        status: str = "loaded",
        validation: Optional[Dict[str, Any]] = None,
    ):
        self.name = name
        self.git_url = git_url
        self.plugin_type = plugin_type  # "task" or "job"
        self.class_name = class_name
        self.path = path
        self.branch = branch
        self.pat = pat  # stored for restart; never exposed in to_dict()
        self.parameters = parameters or {}
        self.start_parameters = start_parameters or {}
        self.pv_definitions = pv_definitions or {}
        self.plugin_prefix = plugin_prefix
        self.auto_start = auto_start
        self.auto_start_on_boot = auto_start_on_boot
        self.autostart_order = autostart_order
        self.status = status
        self.validation = validation
        self.instance: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        mode = None
        if self.plugin_type == "task":
            if self.instance is not None and hasattr(self.instance, "mode"):
                mode = getattr(self.instance, "mode")
            else:
                param_mode = (self.start_parameters or {}).get("mode")
                mode = str(param_mode).lower() if isinstance(param_mode, str) else "continuous"

        base_control_pvs = ["STATUS", "MESSAGE"]
        if self.plugin_type == "task":
            base_control_pvs = ["ENABLE", "STATUS", "MESSAGE"]
            if mode == "triggered":
                base_control_pvs.append("RUN")
            else:
                base_control_pvs.append("CYCLE_COUNT")

        additional_input_pvs = list((self.pv_definitions or {}).get("inputs", {}).keys())
        additional_output_pvs = list((self.pv_definitions or {}).get("outputs", {}).keys())

        built_pvs: List[str] = []
        if self.instance is not None and hasattr(self.instance, "pvs"):
            built_pvs = list(getattr(self.instance, "pvs", {}).keys())
        if not built_pvs:
            built_pvs = list(dict.fromkeys(base_control_pvs + additional_input_pvs + additional_output_pvs))

        d = {
            "name": self.name,
            "git_url": self.git_url,
            "plugin_type": self.plugin_type,
            "class_name": self.class_name,
            "path": self.path,
            "branch": self.branch,
            "status": self.status,
            "auto_start": self.auto_start,
            "auto_start_on_boot": self.auto_start_on_boot,
            "autostart_order": self.autostart_order,
            "pv_prefix": self.instance.pv_prefix if self.instance and hasattr(self.instance, "pv_prefix") else None,
            "plugin_prefix": (
                getattr(self.instance, "plugin_prefix", None)
                if self.instance is not None
                else (self.plugin_prefix or self.name.upper())
            ),
            "mode": mode,
            "parameters": self.parameters,
            "start_parameters": self.start_parameters,
            "pv_definitions": self.pv_definitions,
            "base_control_pvs": base_control_pvs,
            "additional_input_pvs": additional_input_pvs,
            "additional_output_pvs": additional_output_pvs,
            "built_pvs": built_pvs,
        }
        if self.instance and isinstance(self.instance, TaskBase):
            d["running"] = self.instance.running
            d["cycle_count"] = self.instance.cycle_count
        if self.validation is not None:
            d["validation"] = self.validation
        return d


class IocMngController:
    """Main controller managing tasks and jobs lifecycle.

    Provides methods to add/remove/list plugins (tasks & jobs) at runtime.
    Integrates with BeamlineController for optional ophyd/softioc support.
    """

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        beamline_config: Optional[Dict[str, Any]] = None,
        plugins_dir: Optional[Path] = None,
        disable_ophyd: bool = False,
    ):
        self.config = config or {}
        self.beamline_config = beamline_config or {}
        self.disable_ophyd = disable_ophyd
        self.loader = PluginLoader(plugins_dir)
        self._plugins_dir = Path(plugins_dir) if plugins_dir else self.loader.plugins_dir
        self._autostart_registry_path = self._plugins_dir / "autostart_plugins.yaml"
        self._lock = threading.Lock()

        # softIOC lifecycle (API mode): lazily initialized when first plugin
        # builds PVs. This allows REST-loaded tasks/jobs to expose CA PVs.
        env_softioc = os.environ.get("IOCMNG_ENABLE_SOFTIOC")
        if env_softioc is not None:
            self._softioc_enabled = env_softioc.lower() == "true"
        else:
            # Keep API tests isolated even when softioc is installed in the
            # environment. Production deployments still enable softIOC by default.
            self._softioc_enabled = "pytest" not in sys.modules
        self._softioc_initialized = False

        # Loaded plugins: name -> PluginInfo
        self._plugins: Dict[str, PluginInfo] = {}

        # Ophyd devices — lazy singleton dict.
        # Devices are created on first access via get_device().
        self.ophyd_devices: Dict[str, object] = {}
        self._device_index: Dict[str, Dict[str, Any]] = {}
        self._device_factory = None
        logger.debug(
            "IocMngController initialized: config_prefix=%r beamline=%r namespace=%r disable_ophyd=%r plugins_dir=%r",
            self.config.get("prefix"),
            self.beamline_config.get("beamline"),
            self.beamline_config.get("namespace"),
            self.disable_ophyd,
            str(self._plugins_dir),
        )
        if not disable_ophyd and beamline_config:
            self._build_device_index()

    # ------------------------------------------------------------------
    # Ophyd — lazy singleton device creation
    # ------------------------------------------------------------------

    def _build_device_index(self):
        """Pre-compute a lookup table ``device_name -> creation spec`` from
        the ``epicsConfiguration.iocs`` section of the beamline config.

        Each IOC's effective config is obtained by deep-merging its
        ``iocDefaults[template]`` (if present) with the IOC-specific
        overrides so that fields like ``devgroup`` and ``devtype`` are
        inherited automatically.

        No actual devices are created here; they are instantiated lazily by
        :meth:`get_device`.
        """
        ioc_defaults = self.beamline_config.get("iocDefaults", {})
        epics_config = self.beamline_config.get("epicsConfiguration", {})
        iocs = epics_config.get("iocs", [])
        logger.info(f"Building device index from {len(iocs)} IOC definitions "
                     f"({len(ioc_defaults)} iocDefaults templates)")

        for raw_ioc_config in iocs:
            ioc_name = raw_ioc_config.get("name")
            if not ioc_name:
                continue

            # Merge iocDefaults[template] as base, IOC config overrides
            template = raw_ioc_config.get("template", "")
            defaults = ioc_defaults.get(template, {})
            ioc_config = _deep_merge(defaults, raw_ioc_config) if defaults else dict(raw_ioc_config)

            if ioc_config.get("disable", False):
                logger.debug(f"Skipping disabled IOC: {ioc_name}")
                continue

            devgroup = ioc_config.get("devgroup")
            devtype = ioc_config.get("devtype")

            if not devgroup:
                logger.debug(f"IOC {ioc_name} has no devgroup after iocDefaults merge, skipping")
                continue

            ioc_prefix = ioc_config.get("iocprefix", "")
            devices = ioc_config.get("devices", [])

            if devices:
                for device_config in devices:
                    device_name = device_config.get("name")
                    if not device_name:
                        continue
                    if "iocroot" in ioc_config:
                        pv_prefix = f"{ioc_prefix}:{ioc_config['iocroot']}:{device_name}"
                    else:
                        pv_prefix = f"{ioc_prefix}:{device_name}"

                    merged_config = ioc_config.copy()
                    merged_config["iocname"] = ioc_name
                    merged_config.update(device_config)

                    key = device_name
                    if key in self._device_index:
                        key = f"{ioc_name}_{device_name}"
                    self._device_index[key] = {
                        "devgroup": devgroup,
                        "devtype": devtype,
                        "prefix": pv_prefix,
                        "name": device_name,
                        "config": merged_config,
                    }
            else:
                self._device_index[ioc_name] = {
                    "devgroup": devgroup,
                    "devtype": devtype,
                    "prefix": ioc_prefix,
                    "name": ioc_name,
                    "config": ioc_config,
                }

        logger.info(f"Device index built: {len(self._device_index)} devices available for lazy creation")

    def _ensure_factory(self):
        """Import and cache the DeviceFactory singleton."""
        if self._device_factory is None:
            try:
                from infn_ophyd_hal.device_factory import DeviceFactory
                self._device_factory = DeviceFactory()
            except ImportError:
                logger.warning("infn_ophyd_hal not installed — device creation unavailable")
        return self._device_factory

    def get_device(self, device_name: str):
        """Return an Ophyd device by name, creating it on first access (singleton).

        The device is shared across all plugins.  If the device name is not
        found in the beamline index, ``None`` is returned.
        """
        # Fast path — already created
        if device_name in self.ophyd_devices:
            return self.ophyd_devices[device_name]

        spec = self._device_index.get(device_name)
        if spec is None:
            logger.debug(f"Device '{device_name}' not found in device index")
            return None

        factory = self._ensure_factory()
        if factory is None:
            return None

        logger.info(f"Creating device on demand: {device_name} "
                     f"({spec['devgroup']}/{spec['devtype']} prefix={spec['prefix']})")
        try:
            device = factory.create_device(
                devgroup=spec["devgroup"],
                devtype=spec["devtype"],
                prefix=spec["prefix"],
                name=spec["name"],
                config=spec["config"],
            )
        except Exception as e:
            logger.error(f"Failed to create device '{device_name}': {e}", exc_info=True)
            return None

        if device is not None:
            self.ophyd_devices[device_name] = device
        return device

    def list_available_devices(self) -> List[str]:
        """Return names of all devices known in the beamline config."""
        return list(self._device_index.keys())

    # ------------------------------------------------------------------
    # Plugin management
    # ------------------------------------------------------------------

    def _build_and_init_plugin_pvs(self, info: PluginInfo) -> Tuple[bool, str]:
        """Build plugin PV records and initialize softIOC if needed.

        In API mode plugins are loaded dynamically, so we must create records
        at plugin load time (not only in legacy main.py startup flow).
        """
        if not self._softioc_enabled:
            return True, "softIOC disabled"

        try:
            from softioc import builder, softioc
        except ImportError:
            logger.info("softIOC not installed; skipping PV publication for plugin '%s'", info.name)
            return True, "softIOC unavailable"

        instance = info.instance
        if instance is None or not hasattr(instance, "build_pvs"):
            return True, "No PV builder for this plugin"

        try:
            instance.build_pvs()
        except Exception as e:
            return False, f"PV build failed: {e}"

        try:
            # Load records generated so far. For the first plugin we also
            # initialize the IOC runtime.
            builder.LoadDatabase()
            if not self._softioc_initialized:
                softioc.iocInit()
                self._softioc_initialized = True
                logger.info("softIOC initialized in API mode")
        except Exception as e:
            return False, f"softIOC initialization failed: {e}"

        return True, "PVs built"

    def add_plugin(
        self,
        name: str,
        git_url: str,
        pat: Optional[str] = None,
        branch: str = "main",
        path: str = "",
        auto_start: bool = True,
        auto_start_on_boot: bool = False,
        autostart_order: Optional[int] = None,
        parameters: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, str, Optional[Dict]]:
        """Add a task or job plugin from a git repository.

        Steps:
            1. Clone the repository.
            2. Install requirements.txt if present (from *path* or repo root).
            3. Load per-plugin config.yaml from *path* and merge parameters.
            4. Validate (must derive from TaskBase or JobBase, no syntax errors).
            5. Load the class and instantiate.
            6. If it's a task and auto_start is True, start it.

        Args:
            name: Unique name for this plugin.
            git_url: Git repository URL.
            pat: Optional Personal Access Token.
            branch: Branch/tag to clone.
            path: Sub-path inside the repo where the plugin lives.
            auto_start: If True, start tasks immediately.
            parameters: Optional parameters passed to the plugin constructor.
                        These override values from the plugin's config.yaml.

        Returns:
            Tuple of (success, message, validation_dict_or_None).
        """
        created_on_disk = False
        with self._lock:
            if name in self._plugins:
                return False, f"Plugin '{name}' already exists", None

        staged_metadata = self.loader.read_plugin_metadata(name) if self.loader.is_loaded(name) else {}
        staged_plugin_exists = self.loader.is_loaded(name)

        if staged_plugin_exists:
            git_url = git_url or staged_metadata.get("git_url", "")
            branch = branch or staged_metadata.get("branch", "main")
            path = path or staged_metadata.get("source_path", "")
        elif not git_url:
            return False, f"Plugin '{name}' is not staged locally and no git_url was provided", None

        # 1. Clone if the plugin is not already staged locally.
        if not staged_plugin_exists:
            ok, msg = self.loader.clone(name, git_url, pat=pat, branch=branch, path=path, force=True)
            if not ok:
                return False, msg, None
            created_on_disk = True

        # 2. Install requirements
        ok, msg = self.loader.install_requirements(name)
        if not ok:
            if created_on_disk:
                self.loader.remove(name)
            return False, f"Dependency installation failed: {msg}", None

        # 3. Load per-plugin config.yaml
        plugin_config = self.loader.load_plugin_config(name)
        # Merge: REST parameters override config.yaml parameters
        cfg_params = plugin_config.get("parameters", {})
        merged_params = _deep_merge(cfg_params, parameters or {})
        pv_defs = plugin_config.get("pvs", {})
        plugin_prefix = plugin_config.get("prefix")

        # 4. Validate
        validation = self.loader.validate(name)
        if not validation.ok:
            if created_on_disk:
                self.loader.remove(name)
            return False, "Validation failed", validation.to_dict()

        # 5. Load class
        cls, load_result = self.loader.load_class(name)
        if cls is None:
            if created_on_disk:
                self.loader.remove(name)
            return False, "Failed to load class", load_result.to_dict()

        # 6. Instantiate
        try:
            logger.debug(
                "Instantiating plugin: name=%s type=%s controller_prefix=%r plugin_prefix=%r beamline=%r namespace=%r",
                name,
                load_result.plugin_type,
                self.config.get("prefix"),
                plugin_prefix,
                self.beamline_config.get("beamline"),
                self.beamline_config.get("namespace"),
            )
            instance = cls(
                name=name,
                parameters=merged_params,
                pv_definitions=pv_defs,
                beamline_config=self.beamline_config,
                ophyd_devices=self.ophyd_devices,
                prefix=self.config.get("prefix"),
                plugin_prefix=plugin_prefix,
                device_resolver=self.get_device,
            )
        except Exception as e:
            if created_on_disk:
                self.loader.remove(name)
            return False, f"Instantiation failed: {e}", validation.to_dict()

        info = PluginInfo(
            name=name,
            git_url=git_url,
            plugin_type=load_result.plugin_type,
            class_name=load_result.class_name,
            path=path,
            branch=branch,
            pat=pat,
            parameters=parameters or {},
            start_parameters=merged_params,
            pv_definitions=pv_defs,
            plugin_prefix=plugin_prefix,
            auto_start=auto_start,
            auto_start_on_boot=auto_start_on_boot,
            autostart_order=autostart_order,
            status="loaded",
        )
        info.instance = instance

        # Build PVs and ensure softIOC is initialized before starting tasks.
        ok, pv_msg = self._build_and_init_plugin_pvs(info)
        if not ok:
            if created_on_disk:
                self.loader.remove(name)
            return False, pv_msg, validation.to_dict()

        with self._lock:
            self._plugins[name] = info

        logger.info(
            "AS_INFO_LOAD plugin=%s type=%s pv_prefix=%s parameters=%s pv_definitions=%s built_pvs=%s",
            info.name,
            info.plugin_type,
            getattr(instance, "pv_prefix", None),
            merged_params,
            pv_defs,
            info.to_dict().get("built_pvs", []),
        )

        # Auto-start tasks
        if load_result.plugin_type == "task" and auto_start:
            try:
                instance.initialize()
                instance.start()
                info.status = "running"
            except Exception as e:
                info.status = "error"
                return False, f"Start failed: {e}", validation.to_dict()

        if auto_start_on_boot:
            self._upsert_autostart_registry_entry(info)

        self.loader.write_plugin_metadata(
            name,
            {
                "name": name,
                "git_url": git_url,
                "branch": branch,
                "source_path": path,
                "plugin_type": load_result.plugin_type,
                "class_name": load_result.class_name,
                "auto_start": auto_start,
                "auto_start_on_boot": auto_start_on_boot,
                "autostart_order": autostart_order,
            },
        )

        return True, f"Plugin '{name}' added successfully", validation.to_dict()

    def remove_plugin(self, name: str) -> Tuple[bool, str]:
        """Remove a plugin by name, stopping it if running.

        Args:
            name: Plugin name.

        Returns:
            Tuple of (success, message).
        """
        with self._lock:
            info = self._plugins.pop(name, None)

        if info is None:
            ok, _ = self.loader.remove(name)
            if ok:
                self._remove_autostart_registry_entry(name)
                return True, f"Plugin '{name}' removed"
            return False, f"Plugin '{name}' not found"

        # Stop if it's a running task
        if info.instance and isinstance(info.instance, TaskBase):
            try:
                info.instance.stop()
            except Exception as e:
                logger.warning(f"Error stopping task '{name}': {e}")

        # Remove files
        ok, msg = self.loader.remove(name)
        self._remove_autostart_registry_entry(name)
        return True, f"Plugin '{name}' removed"

    def run_job(self, name: str) -> Tuple[bool, Optional[Dict]]:
        """Execute a loaded job.

        Args:
            name: Job name.

        Returns:
            Tuple of (success, result_dict).
        """
        with self._lock:
            info = self._plugins.get(name)

        if info is None:
            return False, {"error": f"Job '{name}' not found"}

        if info.plugin_type != "job":
            return False, {"error": f"'{name}' is a {info.plugin_type}, not a job"}

        result = info.instance.run()
        return result.success, result.to_dict()

    def list_plugins(self, plugin_type: Optional[str] = None) -> List[Dict]:
        """List all loaded plugins.

        Args:
            plugin_type: Optional filter ("task" or "job").

        Returns:
            List of plugin info dicts.
        """
        with self._lock:
            plugins = list(self._plugins.values())

        loaded_names = {plugin.name for plugin in plugins}
        plugins.extend(self._discover_plugins_on_disk(exclude=loaded_names))

        if plugin_type:
            plugins = [p for p in plugins if p.plugin_type == plugin_type]

        return [p.to_dict() for p in plugins]

    def get_plugin(self, name: str) -> Optional[Dict]:
        """Get info about a specific plugin."""
        with self._lock:
            info = self._plugins.get(name)
        if info:
            return info.to_dict()
        discovered = self._discover_plugin_on_disk(name)
        return discovered.to_dict() if discovered else None

    def get_plugin_startup_info(
        self, name: str, expected_type: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Return startup metadata (parameters/PVs) for a loaded plugin."""
        with self._lock:
            info = self._plugins.get(name)
        if info is None:
            info = self._discover_plugin_on_disk(name)
        if info is None:
            return None
        if expected_type and info.plugin_type != expected_type:
            return None

        startup = info.to_dict()
        return {
            "name": startup["name"],
            "plugin_type": startup["plugin_type"],
            "auto_start": startup["auto_start"],
            "auto_start_on_boot": startup["auto_start_on_boot"],
            "autostart_order": startup["autostart_order"],
            "pv_prefix": startup.get("pv_prefix"),
            "plugin_prefix": startup.get("plugin_prefix"),
            "mode": startup.get("mode"),
            "start_parameters": startup["start_parameters"],
            "pv_definitions": startup["pv_definitions"],
            "base_control_pvs": startup["base_control_pvs"],
            "additional_input_pvs": startup["additional_input_pvs"],
            "additional_output_pvs": startup["additional_output_pvs"],
            "built_pvs": startup["built_pvs"],
        }

    def get_task_startup_info(self, name: str) -> Optional[Dict[str, Any]]:
        """Return startup metadata (parameters/PVs) for a loaded task."""
        return self.get_plugin_startup_info(name, expected_type="task")

    def get_job_startup_info(self, name: str) -> Optional[Dict[str, Any]]:
        """Return startup metadata (parameters/PVs) for a loaded job."""
        return self.get_plugin_startup_info(name, expected_type="job")

    def _discover_plugin_on_disk(self, name: str) -> Optional[PluginInfo]:
        if not self.loader.is_loaded(name):
            return None

        metadata = self.loader.read_plugin_metadata(name)
        validation = self.loader.validate(name)
        plugin_config = self.loader.load_plugin_config(name)
        plugin_type = validation.plugin_type or metadata.get("plugin_type") or "unknown"
        class_name = validation.class_name or metadata.get("class_name") or ""

        return PluginInfo(
            name=name,
            git_url=metadata.get("git_url", ""),
            plugin_type=plugin_type,
            class_name=class_name,
            path=metadata.get("source_path", ""),
            branch=metadata.get("branch", "main"),
            parameters={},
            start_parameters=plugin_config.get("parameters", {}),
            pv_definitions=plugin_config.get("pvs", {}),
            plugin_prefix=plugin_config.get("prefix", name.upper()),
            auto_start=metadata.get("auto_start", False),
            auto_start_on_boot=metadata.get("auto_start_on_boot", False),
            autostart_order=metadata.get("autostart_order"),
            status="available" if validation.ok else "invalid",
            validation=validation.to_dict(),
        )

    def _discover_plugins_on_disk(self, exclude: Optional[set[str]] = None) -> List[PluginInfo]:
        exclude = exclude or set()
        discovered: List[PluginInfo] = []
        for name in self.loader.list_local_plugins():
            if name in exclude:
                continue
            info = self._discover_plugin_on_disk(name)
            if info is not None:
                discovered.append(info)
        return discovered

    def stop_all(self):
        """Stop all running tasks."""
        with self._lock:
            plugins = list(self._plugins.values())

        for info in plugins:
            if info.instance and isinstance(info.instance, TaskBase):
                try:
                    info.instance.stop()
                    info.status = "stopped"
                except Exception as e:
                    logger.error(f"Error stopping '{info.name}': {e}")

    # ------------------------------------------------------------------
    # Hot-reload
    # ------------------------------------------------------------------

    def restart_plugin(self, name: str) -> Tuple[bool, str, Optional[Dict]]:
        """Re-fetch, validate, and hot-reload a plugin without downtime.

        Clones the repository into a temporary directory, validates it, and
        only replaces the running instance if every check passes.  The PAT
        and branch used during the original :meth:`add_plugin` call are
        reused automatically.

        Args:
            name: Plugin name to restart.

        Returns:
            Tuple of (success, message, validation_dict_or_None).
        """
        with self._lock:
            info = self._plugins.get(name)
        if info is None:
            return False, f"Plugin '{name}' not found", None

        temp_name = f"__reload__{name}"
        # Clean any leftover temp from a previous failed restart
        if self.loader.plugin_path(temp_name).exists():
            self.loader.remove(temp_name)

        # 1. Clone to temp
        ok, msg = self.loader.clone(temp_name, info.git_url, pat=info.pat, branch=info.branch, path=info.path)
        if not ok:
            return False, f"Re-clone failed: {msg}", None

        # 2. Install requirements in temp
        ok, msg = self.loader.install_requirements(temp_name)
        if not ok:
            self.loader.remove(temp_name)
            return False, f"Dependency installation failed: {msg}", None

        # 3. Load config from temp, merge with original parameters
        plugin_config = self.loader.load_plugin_config(temp_name)
        cfg_params = plugin_config.get("parameters", {})
        merged_params = _deep_merge(cfg_params, info.parameters)
        pv_defs = plugin_config.get("pvs", {})
        plugin_prefix = plugin_config.get("prefix")

        # 4. Validate temp
        validation = self.loader.validate(temp_name)
        if not validation.ok:
            self.loader.remove(temp_name)
            return False, "Validation failed", validation.to_dict()

        # 5. Load class from temp (verify it is importable)
        cls, load_result = self.loader.load_class(temp_name)
        if cls is None:
            self.loader.remove(temp_name)
            return False, "Failed to load class", load_result.to_dict()

        # --- All checks passed: apply the update ---

        # 6. Stop current instance
        was_running = False
        if info.instance and isinstance(info.instance, TaskBase):
            was_running = info.instance.running
            try:
                info.instance.stop()
            except Exception as e:
                logger.warning(f"Error stopping '{name}' during restart: {e}")

        # 7. Swap directories and remove from registry
        with self._lock:
            self._plugins.pop(name, None)
        self.loader.remove(name)
        self.loader.swap_plugin(temp_name, name)

        # 8. Re-instantiate
        try:
            logger.debug(
                "Re-instantiating plugin: name=%s type=%s controller_prefix=%r plugin_prefix=%r beamline=%r namespace=%r",
                name,
                load_result.plugin_type,
                self.config.get("prefix"),
                plugin_prefix,
                self.beamline_config.get("beamline"),
                self.beamline_config.get("namespace"),
            )
            instance = cls(
                name=name,
                parameters=merged_params,
                pv_definitions=pv_defs,
                beamline_config=self.beamline_config,
                ophyd_devices=self.ophyd_devices,
                prefix=self.config.get("prefix"),
                plugin_prefix=plugin_prefix,
                device_resolver=self.get_device,
            )
        except Exception as e:
            return False, f"Instantiation failed after swap: {e}", validation.to_dict()

        new_info = PluginInfo(
            name=name,
            git_url=info.git_url,
            plugin_type=load_result.plugin_type,
            class_name=load_result.class_name,
            path=info.path,
            branch=info.branch,
            pat=info.pat,
            parameters=info.parameters,
            start_parameters=merged_params,
            pv_definitions=pv_defs,
            plugin_prefix=plugin_prefix,
            auto_start=info.auto_start,
            auto_start_on_boot=info.auto_start_on_boot,
            autostart_order=info.autostart_order,
            status="loaded",
        )
        new_info.instance = instance

        with self._lock:
            self._plugins[name] = new_info

        # 9. Restart if it was a running task
        if load_result.plugin_type == "task" and was_running:
            try:
                instance.initialize()
                instance.start()
                new_info.status = "running"
            except Exception as e:
                new_info.status = "error"
                return False, f"Restart failed: {e}", validation.to_dict()

        if new_info.auto_start_on_boot:
            self._upsert_autostart_registry_entry(new_info)

        logger.info(
            "AS_INFO_LOAD plugin=%s type=%s pv_prefix=%s parameters=%s pv_definitions=%s built_pvs=%s",
            new_info.name,
            new_info.plugin_type,
            getattr(instance, "pv_prefix", None),
            merged_params,
            pv_defs,
            new_info.to_dict().get("built_pvs", []),
        )

        self.loader.write_plugin_metadata(
            name,
            {
                "name": name,
                "git_url": info.git_url,
                "branch": info.branch,
                "source_path": info.path,
                "plugin_type": load_result.plugin_type,
                "class_name": load_result.class_name,
                "auto_start": info.auto_start,
                "auto_start_on_boot": info.auto_start_on_boot,
                "autostart_order": info.autostart_order,
            },
        )

        return True, f"Plugin '{name}' restarted successfully", validation.to_dict()

    # ------------------------------------------------------------------
    # Bulk initial load
    # ------------------------------------------------------------------

    def add_plugins_from_config(self, plugins: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Load a list of plugins defined in the initial plugins config.

        Each entry may contain:
            name, git_url, path, branch, pat, auto_start, parameters.

        Args:
            plugins: List of plugin config dicts.

        Returns:
            List of result dicts with ``name``, ``ok``, and ``message``.
        """
        def _sort_key(p: Dict[str, Any]):
            order = p.get("autostart_order")
            if order is None:
                return (1, 0, p.get("name", ""))
            try:
                return (0, int(order), p.get("name", ""))
            except Exception:
                return (1, 0, p.get("name", ""))

        results = []
        for p in sorted(plugins, key=_sort_key):
            name = p.get("name")
            git_url = p.get("git_url")
            if not name or not git_url:
                results.append({"name": name, "ok": False, "message": "Missing name or git_url"})
                continue
            ok, msg, _ = self.add_plugin(
                name=name,
                git_url=git_url,
                pat=p.get("pat"),
                branch=p.get("branch", "main"),
                path=p.get("path", ""),
                auto_start=p.get("auto_start", True),
                auto_start_on_boot=p.get("auto_start_on_boot", False),
                autostart_order=p.get("autostart_order"),
                parameters=p.get("parameters"),
            )
            if ok:
                logger.info(f"Initial plugin '{name}' loaded")
            else:
                logger.error(f"Initial plugin '{name}' failed: {msg}")
            results.append({"name": name, "ok": ok, "message": msg})
        return results

    # ------------------------------------------------------------------
    # Autostart persistence
    # ------------------------------------------------------------------

    def _read_autostart_registry(self) -> List[Dict[str, Any]]:
        if not self._autostart_registry_path.exists():
            return []
        try:
            with open(self._autostart_registry_path, "r") as f:
                data = yaml.safe_load(f) or {}
            return data.get("plugins", [])
        except Exception as e:
            logger.warning("Failed reading autostart registry: %s", e)
            return []

    def _write_autostart_registry(self, plugins: List[Dict[str, Any]]) -> None:
        self._autostart_registry_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._autostart_registry_path, "w") as f:
            yaml.safe_dump({"plugins": plugins}, f, sort_keys=False)

    def _upsert_autostart_registry_entry(self, info: PluginInfo) -> None:
        if info.plugin_type != "task":
            return
        entries = self._read_autostart_registry()
        new_entry = {
            "name": info.name,
            "git_url": info.git_url,
            "path": info.path,
            "branch": info.branch,
            "pat": info.pat,
            "auto_start": True,
            "auto_start_on_boot": True,
            "autostart_order": info.autostart_order,
            "parameters": info.parameters,
        }
        replaced = False
        for i, e in enumerate(entries):
            if e.get("name") == info.name:
                entries[i] = new_entry
                replaced = True
                break
        if not replaced:
            entries.append(new_entry)
        self._write_autostart_registry(entries)

    def _remove_autostart_registry_entry(self, name: str) -> None:
        entries = self._read_autostart_registry()
        filtered = [e for e in entries if e.get("name") != name]
        if len(filtered) != len(entries):
            self._write_autostart_registry(filtered)

    def load_persisted_autostart_plugins(self) -> List[Dict[str, Any]]:
        """Load persisted autostart tasks configured by REST uploads."""
        return self._read_autostart_registry()

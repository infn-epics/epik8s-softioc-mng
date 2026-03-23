"""
Central controller that manages loaded tasks and jobs at runtime.
"""

import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from iocmng.base.task import TaskBase
from iocmng.base.job import JobBase, JobResult
from iocmng.core.loader import PluginLoader
from iocmng.core.validator import ValidationResult

logger = logging.getLogger(__name__)


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
        status: str = "loaded",
    ):
        self.name = name
        self.git_url = git_url
        self.plugin_type = plugin_type  # "task" or "job"
        self.class_name = class_name
        self.path = path
        self.branch = branch
        self.pat = pat  # stored for restart; never exposed in to_dict()
        self.parameters = parameters or {}
        self.status = status
        self.instance: Optional[Any] = None

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "name": self.name,
            "git_url": self.git_url,
            "plugin_type": self.plugin_type,
            "class_name": self.class_name,
            "path": self.path,
            "branch": self.branch,
            "status": self.status,
        }
        if self.instance and isinstance(self.instance, TaskBase):
            d["running"] = self.instance.running
            d["cycle_count"] = self.instance.cycle_count
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
        self._lock = threading.Lock()

        # Loaded plugins: name -> PluginInfo
        self._plugins: Dict[str, PluginInfo] = {}

        # Ophyd devices (loaded externally, optional)
        self.ophyd_devices: Dict[str, object] = {}

    # ------------------------------------------------------------------
    # Plugin management
    # ------------------------------------------------------------------

    def add_plugin(
        self,
        name: str,
        git_url: str,
        pat: Optional[str] = None,
        branch: str = "main",
        path: str = "",
        auto_start: bool = True,
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
        with self._lock:
            if name in self._plugins:
                return False, f"Plugin '{name}' already exists", None

        # 1. Clone
        ok, msg = self.loader.clone(name, git_url, pat=pat, branch=branch)
        if not ok:
            return False, msg, None

        # 2. Install requirements
        ok, msg = self.loader.install_requirements(name, path=path)
        if not ok:
            self.loader.remove(name)
            return False, f"Dependency installation failed: {msg}", None

        # 3. Load per-plugin config.yaml
        plugin_config = self.loader.load_plugin_config(name, path=path)
        # Merge: REST parameters override config.yaml parameters
        cfg_params = plugin_config.get("parameters", {})
        merged_params = _deep_merge(cfg_params, parameters or {})
        pv_defs = plugin_config.get("pvs", {})

        # 4. Validate
        validation = self.loader.validate(name, path=path)
        if not validation.ok:
            self.loader.remove(name)
            return False, "Validation failed", validation.to_dict()

        # 5. Load class
        cls, load_result = self.loader.load_class(name, path=path)
        if cls is None:
            self.loader.remove(name)
            return False, "Failed to load class", load_result.to_dict()

        # 6. Instantiate
        try:
            instance = cls(
                name=name,
                parameters=merged_params,
                pv_definitions=pv_defs,
                beamline_config=self.beamline_config,
                ophyd_devices=self.ophyd_devices,
                prefix=self.config.get("prefix"),
            )
        except Exception as e:
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
            status="loaded",
        )
        info.instance = instance

        with self._lock:
            self._plugins[name] = info

        # Auto-start tasks
        if load_result.plugin_type == "task" and auto_start:
            try:
                instance.initialize()
                instance.start()
                info.status = "running"
            except Exception as e:
                info.status = "error"
                return False, f"Start failed: {e}", validation.to_dict()

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
            return False, f"Plugin '{name}' not found"

        # Stop if it's a running task
        if info.instance and isinstance(info.instance, TaskBase):
            try:
                info.instance.stop()
            except Exception as e:
                logger.warning(f"Error stopping task '{name}': {e}")

        # Remove files
        ok, msg = self.loader.remove(name)
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

        if plugin_type:
            plugins = [p for p in plugins if p.plugin_type == plugin_type]

        return [p.to_dict() for p in plugins]

    def get_plugin(self, name: str) -> Optional[Dict]:
        """Get info about a specific plugin."""
        with self._lock:
            info = self._plugins.get(name)
        return info.to_dict() if info else None

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
        ok, msg = self.loader.clone(temp_name, info.git_url, pat=info.pat, branch=info.branch)
        if not ok:
            return False, f"Re-clone failed: {msg}", None

        # 2. Install requirements in temp
        ok, msg = self.loader.install_requirements(temp_name, path=info.path)
        if not ok:
            self.loader.remove(temp_name)
            return False, f"Dependency installation failed: {msg}", None

        # 3. Load config from temp, merge with original parameters
        plugin_config = self.loader.load_plugin_config(temp_name, path=info.path)
        cfg_params = plugin_config.get("parameters", {})
        merged_params = _deep_merge(cfg_params, info.parameters)
        pv_defs = plugin_config.get("pvs", {})

        # 4. Validate temp
        validation = self.loader.validate(temp_name, path=info.path)
        if not validation.ok:
            self.loader.remove(temp_name)
            return False, "Validation failed", validation.to_dict()

        # 5. Load class from temp (verify it is importable)
        cls, load_result = self.loader.load_class(temp_name, path=info.path)
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
            instance = cls(
                name=name,
                parameters=merged_params,
                pv_definitions=pv_defs,
                beamline_config=self.beamline_config,
                ophyd_devices=self.ophyd_devices,
                prefix=self.config.get("prefix"),
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
        results = []
        for p in plugins:
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
                parameters=p.get("parameters"),
            )
            if ok:
                logger.info(f"Initial plugin '{name}' loaded")
            else:
                logger.error(f"Initial plugin '{name}' failed: {msg}")
            results.append({"name": name, "ok": ok, "message": msg})
        return results

"""
Plugin loader: clones git repositories and loads task/job classes dynamically.

Each plugin repository should contain (at the specified path):
  - A Python file with a class deriving from TaskBase or JobBase
  - A config.yaml with plugin configuration (parameters, pvs, etc.)
  - Optionally a requirements.txt for additional dependencies
"""

import logging
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Type

import yaml

from iocmng.core.validator import PluginValidator, ValidationResult

logger = logging.getLogger(__name__)

PLUGIN_METADATA_FILE = ".iocmng-plugin.json"
CONFIG_FILENAMES = ("config.yaml", "config.yml", "config.json")

# Base directory where plugins are cloned
PLUGINS_DIR = Path(os.environ.get("IOCMNG_PLUGINS_DIR", "/tmp/iocmng_plugins"))


class PluginLoader:
    """Handles cloning, validating, and loading task/job plugins from git."""

    def __init__(self, plugins_dir: Optional[Path] = None):
        self.plugins_dir = plugins_dir or PLUGINS_DIR
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

    def plugin_path(self, name: str) -> Path:
        """Return the local path for a named plugin."""
        return self.plugins_dir / name

    def plugin_source_path(self, name: str, path: str = "") -> Path:
        """Return the source directory inside the cloned repo.

        Args:
            name: Plugin name.
            path: Sub-path inside the repo (e.g. 'src/my_task').

        Returns:
            Resolved path to the plugin sources.
        """
        base = self.plugin_path(name)
        if path:
            return base / path
        return base

    def plugin_metadata_path(self, name: str) -> Path:
        """Return the metadata sidecar path for a named plugin."""
        return self.plugin_path(name) / PLUGIN_METADATA_FILE

    def write_plugin_metadata(self, name: str, metadata: Dict[str, Any]) -> None:
        """Persist lightweight plugin metadata next to the staged plugin files."""
        metadata_path = self.plugin_metadata_path(name)
        metadata_path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")

    def read_plugin_metadata(self, name: str) -> Dict[str, Any]:
        """Load the metadata sidecar for a named plugin if present."""
        metadata_path = self.plugin_metadata_path(name)
        if not metadata_path.exists():
            return {}
        try:
            return json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read plugin metadata for '%s': %s", name, exc)
            return {}

    def list_local_plugins(self) -> List[str]:
        """Return plugin directory names present on disk."""
        if not self.plugins_dir.exists():
            return []
        names: List[str] = []
        for entry in sorted(self.plugins_dir.iterdir()):
            if not entry.is_dir():
                continue
            if entry.name.startswith(".") or entry.name.startswith("__reload__"):
                continue
            names.append(entry.name)
        return names

    def is_loaded(self, name: str) -> bool:
        return self.plugin_path(name).exists()

    def clone(
        self,
        name: str,
        git_url: str,
        pat: Optional[str] = None,
        branch: str = "main",
        path: str = "",
        force: bool = False,
    ) -> Tuple[bool, str]:
        """Clone a git repository for a plugin.

        Args:
            name: Unique plugin name (used as directory name).
            git_url: Git repository URL.
            pat: Optional Personal Access Token for private repos.
            branch: Branch or tag to checkout.
            force: If True, remove any existing directory before cloning
                   (used to recover from a previous failed attempt).

        Returns:
            Tuple of (success, message).
        """
        dest = self.plugin_path(name)

        if dest.exists():
            if force:
                logger.warning("Removing stale plugin directory '%s' before re-clone", dest)
                shutil.rmtree(dest)
            else:
                return False, f"Plugin '{name}' already exists. Remove it first."

        # Construct authenticated URL if PAT provided
        clone_url = git_url
        if pat:
            # Insert token into https URL: https://TOKEN@host/path.git
            if clone_url.startswith("https://"):
                clone_url = clone_url.replace("https://", f"https://{pat}@", 1)
            else:
                logger.warning("PAT provided but URL is not HTTPS; ignoring PAT")

        clone_message = ""

        with tempfile.TemporaryDirectory(prefix=f"iocmng-{name}-") as temp_dir:
            temp_root = Path(temp_dir) / "repo"
            cmd = ["git", "clone", "--depth", "1", "-b", branch, clone_url, str(temp_root)]

            try:
                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )
                if result.returncode != 0:
                    stderr = result.stderr.strip()
                    return False, f"Git clone failed: {stderr}"
            except subprocess.TimeoutExpired:
                return False, "Git clone timed out after 120s"
            except FileNotFoundError:
                return False, "git executable not found"

            selected_root = temp_root / path if path else temp_root
            if not selected_root.exists() or not selected_root.is_dir():
                return False, f"Path '{path}' not found in cloned repository"

            requirements_in_selected_path = selected_root / "requirements.txt"
            requirements_in_repo_root = temp_root / "requirements.txt"

            if path:
                shutil.move(str(selected_root), str(dest))
            else:
                shutil.move(str(temp_root), str(dest))

            if not requirements_in_selected_path.exists() and requirements_in_repo_root.exists():
                shutil.copy2(requirements_in_repo_root, dest / "requirements.txt")

            self.write_plugin_metadata(
                name,
                {
                    "name": name,
                    "git_url": git_url,
                    "branch": branch,
                    "source_path": path,
                },
            )
            clone_message = f"Cloned {git_url} into {dest}"

        return True, clone_message

    def install_requirements(self, name: str, path: str = "") -> Tuple[bool, str]:
        """Install requirements.txt if present in the plugin source directory.

        Looks for requirements.txt first in the sub-path, then at the repo root.

        Args:
            name: Plugin name.
            path: Sub-path inside the repo.

        Returns:
            Tuple of (success, message).
        """
        source_dir = self.plugin_source_path(name, path)
        repo_root = self.plugin_path(name)

        # Check sub-path first, then repo root
        req_file = source_dir / "requirements.txt"
        if not req_file.exists():
            req_file = repo_root / "requirements.txt"
        if not req_file.exists():
            return True, "No requirements.txt found, skipping"

        try:
            result = subprocess.run(
                ["pip", "install", "--no-cache-dir", "-r", str(req_file)],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0:
                return False, f"pip install failed: {result.stderr.strip()}"
            return True, "Requirements installed"
        except subprocess.TimeoutExpired:
            return False, "pip install timed out"
        except FileNotFoundError:
            return False, "pip executable not found"

    def load_plugin_config(self, name: str, path: str = "") -> Dict[str, Any]:
        """Load the plugin's config.yaml from the source directory.

        The config.yaml defines parameters, PV definitions, and other settings
        for the task or job.

        Args:
            name: Plugin name.
            path: Sub-path inside the repo.

        Returns:
            Parsed config dict, or empty dict if no config found.
        """
        source_dir = self.plugin_source_path(name, path)
        config_file = None
        for candidate in CONFIG_FILENAMES:
            candidate_path = source_dir / candidate
            if candidate_path.exists():
                config_file = candidate_path
                break
        if config_file is None:
            logger.debug(f"No config.yaml found in {source_dir}")
            return {}

        try:
            if config_file.suffix == ".json":
                cfg = json.loads(config_file.read_text(encoding="utf-8")) or {}
            else:
                with open(config_file, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f) or {}
            logger.info(f"Loaded plugin config from {config_file}")
            return cfg
        except Exception as e:
            logger.warning(f"Failed to load config.yaml: {e}")
            return {}

    def validate(self, name: str, path: str = "") -> ValidationResult:
        """Validate a cloned plugin.

        Args:
            name: Plugin name.
            path: Sub-path inside the repo.

        Returns:
            ValidationResult.
        """
        source_dir = self.plugin_source_path(name, path)
        if not source_dir.exists():
            return ValidationResult(ok=False, errors=[f"Path '{source_dir}' not found in plugin '{name}'"])

        return PluginValidator.validate_directory(source_dir)

    def load_class(self, name: str, path: str = "") -> Tuple[Optional[Type], ValidationResult]:
        """Validate and load the plugin class.

        Args:
            name: Plugin name.
            path: Sub-path inside the repo.

        Returns:
            Tuple of (class_or_None, ValidationResult).
        """
        result = self.validate(name, path)
        if not result.ok:
            return None, result

        # Find the file with the valid class
        source_dir = self.plugin_source_path(name, path)
        for py_file in sorted(source_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            file_result = PluginValidator.validate_module_path(py_file)
            if file_result.ok and file_result.class_name:
                cls = PluginValidator.load_class(py_file, file_result.class_name)
                return cls, file_result

        return None, ValidationResult(ok=False, errors=["No loadable class found"])

    def remove(self, name: str) -> Tuple[bool, str]:
        """Remove a plugin.

        Args:
            name: Plugin name.

        Returns:
            Tuple of (success, message).
        """
        dest = self.plugin_path(name)
        if not dest.exists():
            return False, f"Plugin '{name}' not found"

        shutil.rmtree(dest)
        return True, f"Plugin '{name}' removed"

    def swap_plugin(self, from_name: str, to_name: str) -> None:
        """Atomically replace *to_name* directory with *from_name* (used for hot-reload).

        The *from_name* directory is renamed to *to_name*, removing any existing
        *to_name* directory first.

        Args:
            from_name: Temp plugin name (source directory).
            to_name: Real plugin name (destination directory).
        """
        src = self.plugin_path(from_name)
        dst = self.plugin_path(to_name)
        if dst.exists():
            shutil.rmtree(dst)
        src.rename(dst)

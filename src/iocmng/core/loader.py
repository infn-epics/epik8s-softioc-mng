"""
Plugin loader: clones git repositories and loads task/job classes dynamically.

Each plugin repository should contain (at the specified path):
  - A Python file with a class deriving from TaskBase or JobBase
  - A config.yaml with plugin configuration (parameters, pvs, etc.)
  - Optionally a requirements.txt for additional dependencies
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Type

import yaml

from iocmng.core.validator import PluginValidator, ValidationResult

logger = logging.getLogger(__name__)

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

    def is_loaded(self, name: str) -> bool:
        return self.plugin_path(name).exists()

    def clone(
        self,
        name: str,
        git_url: str,
        pat: Optional[str] = None,
        branch: str = "main",
    ) -> Tuple[bool, str]:
        """Clone a git repository for a plugin.

        Args:
            name: Unique plugin name (used as directory name).
            git_url: Git repository URL.
            pat: Optional Personal Access Token for private repos.
            branch: Branch or tag to checkout.

        Returns:
            Tuple of (success, message).
        """
        dest = self.plugin_path(name)

        if dest.exists():
            return False, f"Plugin '{name}' already exists. Remove it first."

        # Construct authenticated URL if PAT provided
        clone_url = git_url
        if pat:
            # Insert token into https URL: https://TOKEN@host/path.git
            if clone_url.startswith("https://"):
                clone_url = clone_url.replace("https://", f"https://{pat}@", 1)
            else:
                logger.warning("PAT provided but URL is not HTTPS; ignoring PAT")

        cmd = ["git", "clone", "--depth", "1", "-b", branch, clone_url, str(dest)]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            if result.returncode != 0:
                # Clean up partial clone
                if dest.exists():
                    shutil.rmtree(dest)
                stderr = result.stderr.strip()
                return False, f"Git clone failed: {stderr}"
        except subprocess.TimeoutExpired:
            if dest.exists():
                shutil.rmtree(dest)
            return False, "Git clone timed out after 120s"
        except FileNotFoundError:
            return False, "git executable not found"

        return True, f"Cloned {git_url} into {dest}"

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
        config_file = source_dir / "config.yaml"
        if not config_file.exists():
            # Also try config.yml
            config_file = source_dir / "config.yml"
        if not config_file.exists():
            logger.debug(f"No config.yaml found in {source_dir}")
            return {}

        try:
            with open(config_file, "r") as f:
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

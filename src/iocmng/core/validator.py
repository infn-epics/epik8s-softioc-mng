"""
Validation utilities for dynamically loaded tasks and jobs.

Ensures that user-provided classes properly derive from TaskBase or JobBase,
can be imported, and have all required methods implemented.
"""

import ast
import importlib
import inspect
import json
import logging
import sys
from pathlib import Path
from typing import List, Optional, Tuple, Type

import yaml

from iocmng.base.task import TaskBase
from iocmng.base.job import JobBase

logger = logging.getLogger(__name__)


class ValidationResult:
    """Result of a plugin validation."""

    def __init__(self, ok: bool, class_name: Optional[str] = None, plugin_type: Optional[str] = None, errors: Optional[List[str]] = None):
        self.ok = ok
        self.class_name = class_name
        self.plugin_type = plugin_type  # "task" or "job"
        self.errors = errors or []

    def to_dict(self):
        return {
            "ok": self.ok,
            "class_name": self.class_name,
            "plugin_type": self.plugin_type,
            "errors": self.errors,
        }


class PluginValidator:
    """Validates that a Python module contains a valid iocmng task or job."""

    CONFIG_FILENAMES = ("config.yaml", "config.yml", "config.json")
    VALID_PV_TYPES = {"float", "int", "string", "bool"}

    @staticmethod
    def validate_config_path(directory: Path) -> ValidationResult:
        """Validate optional plugin config file structure.

        Supported formats: YAML and JSON. If no config file exists, validation
        succeeds because runtime defaults are still allowed.
        """
        config_path = None
        for candidate in PluginValidator.CONFIG_FILENAMES:
            candidate_path = directory / candidate
            if candidate_path.exists():
                config_path = candidate_path
                break

        if config_path is None:
            return ValidationResult(ok=True)

        try:
            raw = config_path.read_text(encoding="utf-8")
            if config_path.suffix == ".json":
                config = json.loads(raw) or {}
            else:
                config = yaml.safe_load(raw) or {}
        except Exception as exc:
            return ValidationResult(ok=False, errors=[f"Config parse error in {config_path.name}: {exc}"])

        if not isinstance(config, dict):
            return ValidationResult(ok=False, errors=[f"Config file {config_path.name} must contain a mapping/object at the top level"])

        errors: List[str] = []
        prefix = config.get("prefix")
        if prefix is not None and not isinstance(prefix, str):
            errors.append("Config field 'prefix' must be a string")

        parameters = config.get("parameters", {})
        if parameters is not None and not isinstance(parameters, dict):
            errors.append("Config field 'parameters' must be a mapping/object")

        pvs = config.get("pvs", {})
        if pvs is not None and not isinstance(pvs, dict):
            errors.append("Config field 'pvs' must be a mapping/object")
        elif isinstance(pvs, dict):
            for section_name in ("inputs", "outputs"):
                section = pvs.get(section_name, {})
                if section is None:
                    continue
                if not isinstance(section, dict):
                    errors.append(f"Config pvs.{section_name} must be a mapping/object")
                    continue
                for pv_name, pv_cfg in section.items():
                    if not isinstance(pv_cfg, dict):
                        errors.append(f"PV '{pv_name}' in pvs.{section_name} must be a mapping/object")
                        continue
                    pv_type = pv_cfg.get("type")
                    if pv_type is not None and pv_type not in PluginValidator.VALID_PV_TYPES:
                        errors.append(
                            f"PV '{pv_name}' in pvs.{section_name} has invalid type '{pv_type}'"
                        )

        if errors:
            return ValidationResult(ok=False, errors=errors)
        return ValidationResult(ok=True)

    @staticmethod
    def validate_module_path(module_path: Path) -> ValidationResult:
        """Validate a Python module file.

        Checks:
            1. File exists and is a .py file.
            2. File parses without syntax errors.
            3. Module can be imported.
            4. Module contains at least one class deriving from TaskBase or JobBase.
            5. All abstract methods are implemented.

        Args:
            module_path: Path to the Python module file.

        Returns:
            ValidationResult with ok=True if valid, or errors list if not.
        """
        errors: List[str] = []

        # 1. File existence
        if not module_path.exists():
            return ValidationResult(ok=False, errors=[f"File not found: {module_path}"])
        if not module_path.suffix == ".py":
            return ValidationResult(ok=False, errors=[f"Not a Python file: {module_path}"])

        # 2. Syntax check via AST
        try:
            source = module_path.read_text(encoding="utf-8")
            ast.parse(source, filename=str(module_path))
        except SyntaxError as e:
            return ValidationResult(ok=False, errors=[f"Syntax error: {e}"])

        # 3. Try to import
        module_name = module_path.stem
        parent_dir = str(module_path.parent)
        added_to_path = False
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
            added_to_path = True

        try:
            # Force reimport if already cached
            if module_name in sys.modules:
                del sys.modules[module_name]
            module = importlib.import_module(module_name)
        except SystemExit as e:
            if added_to_path:
                sys.path.remove(parent_dir)
            return ValidationResult(ok=False, errors=[f"Import error: module calls sys.exit({e.code}) at import time"])
        except Exception as e:
            if added_to_path:
                sys.path.remove(parent_dir)
            return ValidationResult(ok=False, errors=[f"Import error: {e}"])

        # 4. Find valid classes
        found_class = None
        plugin_type = None

        for name, obj in inspect.getmembers(module, inspect.isclass):
            # Skip the base classes themselves
            if obj in (TaskBase, JobBase):
                continue

            if issubclass(obj, TaskBase):
                found_class = name
                plugin_type = "task"
                break
            elif issubclass(obj, JobBase):
                found_class = name
                plugin_type = "job"
                break

        if found_class is None:
            errors.append(
                "No class found that derives from iocmng.TaskBase or iocmng.JobBase"
            )
            if added_to_path:
                sys.path.remove(parent_dir)
            return ValidationResult(ok=False, errors=errors)

        # 5. Check abstract methods are implemented
        cls = getattr(module, found_class)
        abstract_methods = set()
        for klass in inspect.getmro(cls):
            if hasattr(klass, "__abstractmethods__"):
                abstract_methods |= klass.__abstractmethods__

        # If the class itself still has unimplemented abstract methods
        if getattr(cls, "__abstractmethods__", set()):
            remaining = cls.__abstractmethods__
            errors.append(
                f"Abstract methods not implemented: {', '.join(remaining)}"
            )
            if added_to_path:
                sys.path.remove(parent_dir)
            return ValidationResult(ok=False, class_name=found_class, plugin_type=plugin_type, errors=errors)

        if added_to_path:
            sys.path.remove(parent_dir)

        return ValidationResult(ok=True, class_name=found_class, plugin_type=plugin_type)

    @staticmethod
    def validate_directory(directory: Path) -> ValidationResult:
        """Validate a directory containing plugin module(s).

        Looks for Python files and validates each until a valid one is found.
        A valid plugin directory must have at least one .py file with a class
        deriving from TaskBase or JobBase.

        Args:
            directory: Path to the plugin directory.

        Returns:
            ValidationResult for the first valid plugin found, or errors.
        """
        if not directory.is_dir():
            return ValidationResult(ok=False, errors=[f"Not a directory: {directory}"])

        config_result = PluginValidator.validate_config_path(directory)
        if not config_result.ok:
            return config_result

        py_files = sorted(directory.glob("*.py"))
        if not py_files:
            return ValidationResult(ok=False, errors=["No Python files found in directory"])

        all_errors: List[str] = []
        for py_file in py_files:
            if py_file.name.startswith("_"):
                continue
            result = PluginValidator.validate_module_path(py_file)
            if result.ok:
                return result
            all_errors.extend([f"{py_file.name}: {e}" for e in result.errors])

        return ValidationResult(
            ok=False,
            errors=["No valid task/job class found in any module"] + all_errors,
        )

    @staticmethod
    def load_class(module_path: Path, class_name: str) -> Type:
        """Import and return a class from a module path.

        Args:
            module_path: Path to the Python module.
            class_name: Name of the class to load.

        Returns:
            The class object.
        """
        module_name = module_path.stem
        parent_dir = str(module_path.parent)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)

        if module_name in sys.modules:
            del sys.modules[module_name]

        module = importlib.import_module(module_name)
        return getattr(module, class_name)

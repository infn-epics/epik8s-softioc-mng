"""Shared plugin configuration normalization for tasks and jobs."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional


VALID_PV_TYPES = {"float", "int", "string", "bool"}


def deep_merge_dicts(base: Optional[Mapping[str, Any]], override: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Recursively merge *override* into *base*, returning a new dict."""
    result = dict(base or {})
    for key, value in dict(override or {}).items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge_dicts(result[key], value)
        else:
            result[key] = value
    return result


def _mapping(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def normalize_argument_sections(config: Optional[Mapping[str, Any]]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Normalize plugin PV arguments.

    Preferred schema:

    arguments:
      inputs: ...
      outputs: ...

    Legacy schema remains supported:

    pvs:
      inputs: ...
      outputs: ...
    """
    raw_config = _mapping(config)
    legacy_sections = _mapping(raw_config.get("pvs"))
    argument_sections = _mapping(raw_config.get("arguments"))

    normalized: Dict[str, Dict[str, Dict[str, Any]]] = {"inputs": {}, "outputs": {}}
    for section_name in ("inputs", "outputs"):
        merged = {}
        merged.update(_mapping(legacy_sections.get(section_name)))
        merged.update(_mapping(argument_sections.get(section_name)))
        normalized[section_name] = {
            str(name): dict(spec)
            for name, spec in merged.items()
            if isinstance(spec, Mapping)
        }
    return normalized


@dataclass(frozen=True)
class PvArgumentSpec:
    """Normalized definition for a plugin input or output PV."""

    name: str
    direction: str
    type: str = "float"
    value: Any = 0
    unit: str = ""
    prec: int = 3
    low: Any = 0
    high: Any = 100
    znam: str = "Off"
    onam: str = "On"
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(cls, name: str, direction: str, config: Optional[Mapping[str, Any]] = None) -> "PvArgumentSpec":
        raw = _mapping(config)
        pv_type = str(raw.get("type", "float")).lower()
        if pv_type not in VALID_PV_TYPES:
            pv_type = "float"
        return cls(
            name=name,
            direction=direction,
            type=pv_type,
            value=raw.get("value", 0),
            unit=str(raw.get("unit", "")),
            prec=int(raw.get("prec", 3)),
            low=raw.get("low", 0),
            high=raw.get("high", 100),
            znam=str(raw.get("znam", "Off")),
            onam=str(raw.get("onam", "On")),
            raw=dict(raw),
        )

    @property
    def writable(self) -> bool:
        return self.direction == "input"

    def to_dict(self) -> Dict[str, Any]:
        normalized = dict(self.raw)
        normalized.setdefault("type", self.type)
        normalized.setdefault("value", self.value)
        if self.type == "float":
            normalized.setdefault("unit", self.unit)
            normalized.setdefault("prec", self.prec)
            normalized.setdefault("low", self.low)
            normalized.setdefault("high", self.high)
        if self.type == "bool":
            normalized.setdefault("znam", self.znam)
            normalized.setdefault("onam", self.onam)
        return normalized


@dataclass(frozen=True)
class PluginSpec:
    """Normalized plugin configuration consumed by tasks, jobs, and the controller."""

    prefix: Optional[str]
    parameters: Dict[str, Any]
    inputs: Dict[str, PvArgumentSpec]
    outputs: Dict[str, PvArgumentSpec]
    raw_config: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_config(
        cls,
        config: Optional[Mapping[str, Any]] = None,
        parameters_override: Optional[Mapping[str, Any]] = None,
        default_prefix: Optional[str] = None,
    ) -> "PluginSpec":
        raw_config = _mapping(config)
        arguments = normalize_argument_sections(raw_config)
        parameters = deep_merge_dicts(_mapping(raw_config.get("parameters")), parameters_override)
        prefix = raw_config.get("prefix") or default_prefix
        return cls(
            prefix=prefix,
            parameters=parameters,
            inputs={
                name: PvArgumentSpec.from_config(name, "input", spec)
                for name, spec in arguments["inputs"].items()
            },
            outputs={
                name: PvArgumentSpec.from_config(name, "output", spec)
                for name, spec in arguments["outputs"].items()
            },
            raw_config=dict(raw_config),
        )

    @classmethod
    def from_runtime(
        cls,
        parameters: Optional[Mapping[str, Any]] = None,
        pv_definitions: Optional[Mapping[str, Any]] = None,
        plugin_prefix: Optional[str] = None,
    ) -> "PluginSpec":
        config = {
            "prefix": plugin_prefix,
            "parameters": dict(parameters or {}),
            "arguments": {
                "inputs": _mapping(_mapping(pv_definitions).get("inputs")),
                "outputs": _mapping(_mapping(pv_definitions).get("outputs")),
            },
        }
        return cls.from_config(config=config, default_prefix=plugin_prefix)

    @property
    def arguments(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        return {
            "inputs": {name: spec.to_dict() for name, spec in self.inputs.items()},
            "outputs": {name: spec.to_dict() for name, spec in self.outputs.items()},
        }

    @property
    def pv_definitions(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        return self.arguments


def create_softioc_record(spec: PvArgumentSpec, on_update=None):
    """Create a softIOC record for a normalized PV argument definition."""
    from softioc import builder

    if spec.type == "float":
        kwargs = dict(
            initial_value=float(spec.value),
            EGU=spec.unit,
            PREC=spec.prec,
            LOPR=spec.low,
            HOPR=spec.high,
        )
        if spec.writable:
            return builder.aOut(spec.name, on_update=on_update, **kwargs)
        return builder.aIn(spec.name, **kwargs)

    if spec.type == "int":
        if spec.writable:
            return builder.longOut(spec.name, initial_value=int(spec.value), on_update=on_update)
        return builder.longIn(spec.name, initial_value=int(spec.value))

    if spec.type == "string":
        if spec.writable:
            return builder.stringOut(spec.name, initial_value=str(spec.value), on_update=on_update)
        return builder.stringIn(spec.name, initial_value=str(spec.value))

    if spec.type == "bool":
        kwargs = dict(
            initial_value=int(spec.value),
            ZNAM=spec.znam,
            ONAM=spec.onam,
        )
        if spec.writable:
            return builder.boolOut(spec.name, on_update=on_update, **kwargs)
        return builder.boolIn(spec.name, **kwargs)

    if spec.writable:
        return builder.aOut(spec.name, initial_value=float(spec.value), on_update=on_update)
    return builder.aIn(spec.name, initial_value=float(spec.value))
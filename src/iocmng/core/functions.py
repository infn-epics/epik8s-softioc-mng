"""Built-in function library for declarative expressions.

Functions registered here are available in rule conditions, transforms,
and any other :func:`~iocmng.core.safe_eval.safe_eval` context.

The registry is extensible — plugins can add custom functions via
:func:`register`.

Categories::

    math        — abs, round, sqrt, log, exp, pow, floor, ceil, clamp
    statistics  — mean, std, variance, median, rms
    logic       — any_of, all_of, count_true
    array       — length, sum_of, diff, last, min, max

Usage in a config.yaml rule::

    rules:
      - id: HIGH_NOISE
        condition: "std(signal_buf) > 0.5"
        outputs: {ALARM: 1}

    transforms:
      - output: avg_temp
        expression: "mean(temp_buf)"
"""

from __future__ import annotations

import math as _math
from collections.abc import Sequence as _Seq
from typing import Any, Callable, Dict, List

_REGISTRY: Dict[str, Callable] = {}


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def register(name: str, fn: Callable) -> None:
    """Register *fn* as a safe function callable from expressions."""
    _REGISTRY[name] = fn


def get_registry() -> Dict[str, Callable]:
    """Return a snapshot of the current function registry."""
    return dict(_REGISTRY)


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _as_list(v: Any) -> List[Any]:
    """Coerce *v* to a flat list — scalars become ``[v]``."""
    if isinstance(v, (list, tuple)):
        return list(v)
    if isinstance(v, _Seq) and not isinstance(v, (str, bytes)):
        return list(v)
    return [v]


# ------------------------------------------------------------------
# Math
# ------------------------------------------------------------------

register("abs", abs)
register("round", round)
register("sqrt", _math.sqrt)
register("log", _math.log)
register("exp", _math.exp)
register("pow", pow)
register("floor", _math.floor)
register("ceil", _math.ceil)


def _clamp(value, low, high):
    """Clamp *value* between *low* and *high*."""
    return max(low, min(high, value))


register("clamp", _clamp)


# ------------------------------------------------------------------
# Statistics  (operate on scalars *and* arrays transparently)
# ------------------------------------------------------------------

def _mean(values):
    vals = _as_list(values)
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _std(values):
    vals = _as_list(values)
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return _math.sqrt(sum((x - m) ** 2 for x in vals) / len(vals))


def _variance(values):
    vals = _as_list(values)
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return sum((x - m) ** 2 for x in vals) / len(vals)


def _median(values):
    vals = sorted(_as_list(values))
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2 == 0:
        return (vals[mid - 1] + vals[mid]) / 2.0
    return vals[mid]


def _rms(values):
    vals = _as_list(values)
    if not vals:
        return 0.0
    return _math.sqrt(sum(x ** 2 for x in vals) / len(vals))


register("mean", _mean)
register("std", _std)
register("variance", _variance)
register("median", _median)
register("rms", _rms)

# Built-in min/max work on iterables *and* varargs — perfect.
register("min", min)
register("max", max)


# ------------------------------------------------------------------
# Logic  (for composing boolean sub-expressions)
# ------------------------------------------------------------------

def _any_of(*args):
    """Return True if any argument is truthy."""
    return any(bool(a) for a in args)


def _all_of(*args):
    """Return True if all arguments are truthy."""
    return all(bool(a) for a in args)


def _count_true(*args):
    """Count how many arguments are truthy."""
    return sum(1 for a in args if bool(a))


register("any_of", _any_of)
register("all_of", _all_of)
register("count_true", _count_true)


# ------------------------------------------------------------------
# Array / buffer helpers
# ------------------------------------------------------------------

def _length(values):
    """Number of elements in *values*."""
    return len(_as_list(values))


def _sum_of(values):
    """Sum of elements in *values*."""
    return sum(_as_list(values))


def _diff(values):
    """First-order differences of *values*."""
    vals = _as_list(values)
    return [vals[i] - vals[i - 1] for i in range(1, len(vals))]


def _last(values, n=1):
    """Return the last *n* elements of *values*."""
    vals = _as_list(values)
    return vals[-n:]


def _moving_avg(values, window=None):
    """Simple moving average over the last *window* elements.

    If *window* is ``None`` the entire buffer is averaged.
    """
    vals = _as_list(values)
    if window is not None:
        vals = vals[-int(window):]
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _derivative(values):
    """Approximate derivative (first-order diff)."""
    return _diff(values)


register("length", _length)
register("sum_of", _sum_of)
register("diff", _diff)
register("last", _last)
register("moving_avg", _moving_avg)
register("derivative", _derivative)

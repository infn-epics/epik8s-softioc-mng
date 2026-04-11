"""Thin wrapper around p4p for get / put / monitor on external PVs.

A global *provider* setting (``"pva"`` or ``"ca"``) is initialised once via
:func:`init` and shared by all callers.  The underlying :class:`p4p.client.thread.Context`
is created lazily on first use so that import-time side-effects are avoided.
"""

import logging
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ── Global state ──────────────────────────────────────────────────────

_provider: str = "pva"        # "pva" or "ca"
_context = None                # p4p.client.thread.Context (lazy)
_lock = threading.Lock()
_subscriptions: Dict[str, Any] = {}  # name -> p4p Subscription


def init(pva: bool = True) -> None:
    """Set the default provider.  Call once at startup.

    Args:
        pva: If *True* (default) use PV Access (``"pva"``); otherwise use
             Channel Access (``"ca"``).
    """
    global _provider, _context
    with _lock:
        _provider = "pva" if pva else "ca"
        _context = None  # force re-creation on next use
        logger.info("PV client provider set to %r", _provider)


def get_provider() -> str:
    """Return the current provider string (``"pva"`` or ``"ca"``)."""
    return _provider


def _get_context():
    """Return (or lazily create) the shared :class:`p4p.client.thread.Context`."""
    global _context
    if _context is not None:
        return _context
    with _lock:
        if _context is None:
            from p4p.client.thread import Context
            _context = Context(_provider)
            logger.info("Created p4p Context(provider=%r)", _provider)
        return _context


# ── Public API ────────────────────────────────────────────────────────

def get(pv_name: str, timeout: float = 5.0) -> Any:
    """Get the current value of an external PV.

    Args:
        pv_name: Full PV name (e.g. ``"BEAM:CURRENT"``).
        timeout: Timeout in seconds.

    Returns:
        The PV value.  For PVA this is typically a :class:`p4p.Value`;
        for CA a plain Python scalar or array.
    """
    ctx = _get_context()
    value = ctx.get(pv_name, timeout=timeout)
    logger.debug("get(%s) -> %s", pv_name, value)
    return value


def put(pv_name: str, value: Any, timeout: float = 5.0) -> None:
    """Write a value to an external PV.

    Args:
        pv_name: Full PV name.
        value: The value to write.
        timeout: Timeout in seconds.
    """
    ctx = _get_context()
    ctx.put(pv_name, value, timeout=timeout)
    logger.debug("put(%s, %s)", pv_name, value)


def monitor(
    pv_name: str,
    callback: Callable[[Any], None],
    name: Optional[str] = None,
) -> str:
    """Start a subscription (monitor) on an external PV.

    Args:
        pv_name: Full PV name.
        callback: Called with each new value.
        name: Optional friendly name used to identify the subscription for
              later cancellation.  Defaults to *pv_name*.

    Returns:
        The subscription key (use with :func:`unmonitor`).
    """
    key = name or pv_name
    ctx = _get_context()
    sub = ctx.monitor(pv_name, callback)
    with _lock:
        # Close any previous subscription under the same key.
        old = _subscriptions.pop(key, None)
        if old is not None:
            old.close()
        _subscriptions[key] = sub
    logger.info("monitor(%s) started (key=%r)", pv_name, key)
    return key


def unmonitor(key: str) -> bool:
    """Cancel a previously started subscription.

    Args:
        key: The key returned by :func:`monitor`.

    Returns:
        *True* if the subscription existed and was closed, *False* otherwise.
    """
    with _lock:
        sub = _subscriptions.pop(key, None)
    if sub is not None:
        sub.close()
        logger.info("unmonitor(%s) closed", key)
        return True
    return False


def unmonitor_all() -> int:
    """Cancel **all** active subscriptions.

    Returns:
        Number of subscriptions that were closed.
    """
    with _lock:
        subs = dict(_subscriptions)
        _subscriptions.clear()
    for sub in subs.values():
        sub.close()
    if subs:
        logger.info("unmonitor_all: closed %d subscription(s)", len(subs))
    return len(subs)


def active_monitors() -> Dict[str, str]:
    """Return a snapshot ``{key: pv_name}`` of active subscriptions."""
    with _lock:
        return {k: str(v) for k, v in _subscriptions.items()}


def close() -> None:
    """Tear down all subscriptions and the shared context."""
    global _context
    unmonitor_all()
    with _lock:
        if _context is not None:
            _context.close()
            _context = None
    logger.info("PV client closed")

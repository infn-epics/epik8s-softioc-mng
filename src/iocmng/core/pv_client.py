"""Thin wrapper around p4p (PVA) or PyEPICS (CA) for get / put / monitor on external PVs.

A global *provider* setting (``"pva"`` or ``"ca"``) is initialised once via
:func:`init` and shared by all callers.

- PVA: uses :class:`p4p.client.thread.Context` (lazy singleton)
- CA:  uses ``epics`` (PyEPICS) — avoids p4p CA issues with native CA IOCs
"""

import logging
import threading
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# ── Global state ──────────────────────────────────────────────────────

_provider: str = "pva"        # "pva" or "ca"
_context = None                # p4p Context, only used for PVA
_lock = threading.Lock()
_subscriptions: Dict[str, Any] = {}  # key -> p4p Subscription or epics.PV


def init(pva: bool = True) -> None:
    """Set the default provider.  Call once at startup.

    Args:
        pva: If *True* (default) use PV Access (``"pva"``); otherwise use
             Channel Access via PyEPICS (``"ca"``).
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
    """Return (or lazily create) the shared p4p Context. Only used for PVA."""
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
    """Get the current value of an external PV."""
    if _provider == "ca":
        import epics
        value = epics.caget(pv_name, timeout=timeout)
        if value is None:
            raise TimeoutError(f"caget timed out for {pv_name!r}")
        logger.debug("get(%s) -> %s", pv_name, value)
        return value
    ctx = _get_context()
    value = ctx.get(pv_name, timeout=timeout)
    logger.debug("get(%s) -> %s", pv_name, value)
    return value


def put(pv_name: str, value: Any, timeout: float = 5.0) -> None:
    """Write a value to an external PV."""
    if _provider == "ca":
        import epics
        result = epics.caput(pv_name, value, timeout=timeout)
        if not result:
            raise TimeoutError(f"caput timed out for {pv_name!r}")
        logger.debug("put(%s, %s)", pv_name, value)
        return
    ctx = _get_context()
    ctx.put(pv_name, value, timeout=timeout)
    logger.debug("put(%s, %s)", pv_name, value)


def monitor(
    pv_name: str,
    callback: Callable[[Any], None],
    name: Optional[str] = None,
    conn_callback: Optional[Callable[[bool], None]] = None,
) -> str:
    """Start a subscription (monitor) on an external PV.

    Args:
        pv_name: The PV to subscribe to.
        callback: Called with the new value on each update.
        name: Optional subscription key (defaults to *pv_name*).
        conn_callback: Optional callback invoked with ``True`` on connect
            and ``False`` on disconnect.  Supported for both CA and PVA.

    Returns:
        The subscription key (use with :func:`unmonitor`).
    """
    key = name or pv_name

    if _provider == "ca":
        import epics

        def _ca_callback(value=None, **kwargs):
            if value is not None:
                callback(value)

        pv = epics.PV(pv_name)
        if conn_callback is not None:
            pv.connection_callbacks.append(
                lambda pvname=None, conn=None, **kw: conn_callback(bool(conn))
            )
        pv.add_callback(_ca_callback)
        with _lock:
            old = _subscriptions.pop(key, None)
            if old is not None:
                _close_subscription(old)
            _subscriptions[key] = pv
        logger.info("monitor(%s) started via PyEPICS (key=%r)", pv_name, key)
        return key

    ctx = _get_context()

    if conn_callback is not None:
        # p4p: wrap callback to detect Disconnected events
        def _pva_value_cb(value):
            try:
                from p4p.nt import NTScalar  # noqa: F401
                # p4p delivers Disconnected as a special type
                if isinstance(value, Exception):
                    conn_callback(False)
                    return
                conn_callback(True)
                callback(value)
            except Exception:
                callback(value)

        sub = ctx.monitor(pv_name, _pva_value_cb, notify_disconnect=True)
    else:
        sub = ctx.monitor(pv_name, callback)

    with _lock:
        old = _subscriptions.pop(key, None)
        if old is not None:
            _close_subscription(old)
        _subscriptions[key] = sub
    logger.info("monitor(%s) started via p4p (key=%r)", pv_name, key)
    return key


def _close_subscription(sub: Any) -> None:
    """Close a subscription regardless of type (p4p or epics.PV)."""
    try:
        import epics
        if isinstance(sub, epics.PV):
            sub.clear_callbacks()
            sub.disconnect()
            return
    except ImportError:
        pass
    # p4p subscription
    sub.close()


def unmonitor(key: str) -> bool:
    """Cancel a previously started subscription."""
    with _lock:
        sub = _subscriptions.pop(key, None)
    if sub is not None:
        _close_subscription(sub)
        logger.info("unmonitor(%s) closed", key)
        return True
    return False


def unmonitor_all() -> int:
    """Cancel **all** active subscriptions."""
    with _lock:
        subs = dict(_subscriptions)
        _subscriptions.clear()
    for sub in subs.values():
        _close_subscription(sub)
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

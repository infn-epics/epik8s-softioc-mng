"""Start the pythonSoftIOC runtime with the chosen dispatcher.

The dispatcher runs record callbacks (``on_update``).  iocmng uses
pythonSoftIOC's asyncio dispatcher by default: it is part of the standard
library and builds everywhere.  ``cothread`` is still available for plugins
that depend on it: set ``IOCMNG_DISPATCHER=cothread`` and install
``iocmng[cothread]``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Optional

logger = logging.getLogger(__name__)

DISPATCHERS = ("asyncio", "cothread")
ENV_VAR = "IOCMNG_DISPATCHER"

# Kept so the dispatcher (and its event loop thread) lives as long as the IOC.
_dispatcher: Any = None


def dispatcher_name(name: Optional[str] = None) -> str:
    """Return the dispatcher to use: *name*, else ``$IOCMNG_DISPATCHER``, else asyncio."""
    chosen = (name or os.environ.get(ENV_VAR) or "asyncio").strip().lower()
    if chosen not in DISPATCHERS:
        raise ValueError(
            f"unknown dispatcher {chosen!r} ({ENV_VAR}); expected one of {', '.join(DISPATCHERS)}"
        )
    return chosen


def make_dispatcher(name: Optional[str] = None) -> Any:
    """Create the dispatcher for ``softioc.iocInit``.

    Returns ``None`` for cothread, which is pythonSoftIOC's own default.
    """
    chosen = dispatcher_name(name)
    if chosen == "cothread":
        try:
            import cothread  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                f"{ENV_VAR}=cothread but cothread is not installed: pip install 'iocmng[cothread]'"
            ) from exc
        return None
    from softioc import asyncio_dispatcher

    return asyncio_dispatcher.AsyncioDispatcher()


def ioc_init(name: Optional[str] = None) -> str:
    """Call ``softioc.iocInit`` with the chosen dispatcher; return its name."""
    global _dispatcher
    from softioc import softioc

    chosen = dispatcher_name(name)
    _dispatcher = make_dispatcher(chosen)
    if _dispatcher is None:
        softioc.iocInit()
    else:
        softioc.iocInit(_dispatcher)
    logger.info("softIOC initialized with the %s dispatcher", chosen)
    return chosen

"""
ChannelFinder integration for iocmng tasks.

Provides a lightweight client that wraps the ChannelFinder REST API and
device-creation helpers.  The client is activated **only** when a
``channelfinder_url`` parameter is supplied in the task configuration;
otherwise all methods gracefully return empty results.

Properties stored by ``channelfinder-service-feeder`` and queryable here:

    iocName, beamline, devgroup, devtype, device, zone, zones,
    iocprefix, iocroot, template, host, server, ioc_version,
    pvProtocol, lastUpdated, desc

Example usage inside a :class:`~iocmng.base.task.TaskBase`::

    def initialize(self):
        motors = self.cf_search(devgroup="mot", devtype="tml", name="SPARC:MOT:TML:*")
        for ch in motors:
            dev = self.cf_create_device(ch)
            if dev:
                self.logger.info("Created %s -> %s", dev.name, dev.prefix)
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

try:
    import requests

    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False


class ChannelFinderClient:
    """Minimal read-only ChannelFinder REST client.

    Parameters
    ----------
    url : str
        Base URL, e.g. ``http://cf-host:8080/ChannelFinder``.
    timeout : float
        HTTP request timeout in seconds.
    """

    def __init__(self, url: str, timeout: float = 10.0):
        if not _HAS_REQUESTS:
            raise ImportError(
                "The 'requests' package is required for ChannelFinder support. "
                "Install it with:  pip install requests"
            )
        self.base_url = url.rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()

    # ------------------------------------------------------------------
    # low-level
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Any:
        url = f"{self.base_url}/resources/{path}"
        resp = self._session.get(url, params=params, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def is_available(self) -> bool:
        try:
            self._session.get(self.base_url, timeout=self.timeout)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def search(
        self,
        *,
        name: Optional[str] = None,
        iocName: Optional[str] = None,
        devgroup: Optional[str] = None,
        devtype: Optional[str] = None,
        zone: Optional[str] = None,
        tag: Optional[str] = None,
        size: int = 10000,
        **extra_filters: str,
    ) -> List[Dict[str, Any]]:
        """Search channels by property filters.

        All filter values support ChannelFinder glob patterns (``*``, ``?``).

        Returns a list of channel dicts with ``name``, ``properties``, ``tags``.
        """
        params: Dict[str, str] = {"~size": str(size)}
        if name:
            params["~name"] = name
        if tag:
            params["~tag"] = tag
        if iocName:
            params["iocName"] = iocName
        if devgroup:
            params["devgroup"] = devgroup
        if devtype:
            params["devtype"] = devtype
        if zone:
            params["zone"] = zone
        for k, v in extra_filters.items():
            params[k] = v
        return self._get("channels", params=params)

    def get_channel(self, channel_name: str) -> Dict[str, Any]:
        return self._get(f"channels/{channel_name}")

    # ------------------------------------------------------------------
    # device discovery
    # ------------------------------------------------------------------

    def discover_devices(
        self,
        *,
        name: Optional[str] = None,
        iocName: Optional[str] = None,
        devgroup: Optional[str] = None,
        devtype: Optional[str] = None,
        zone: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Group matching channels into device descriptors.

        Channels are grouped by their PV stem (prefix without the
        trailing field/suffix segment).  Each returned dict contains::

            {"name":     "GUNFLG01",
             "devgroup": "mot",
             "devtype":  "tml",
             "prefix":   "SPARC:MOT:TML:GUNFLG01",
             "iocname":  "tml-ch1",
             "properties": { ... },
             "pvs":      ["SPARC:MOT:TML:GUNFLG01:RBV", ...]}

        These dicts can be passed directly to
        :meth:`~iocmng.base.task.TaskBase.cf_create_device`.
        """
        channels = self.search(
            name=name,
            iocName=iocName,
            devgroup=devgroup,
            devtype=devtype,
            zone=zone,
        )

        devices: Dict[str, Dict[str, Any]] = {}
        for ch in channels:
            props = {p["name"]: p.get("value", "") for p in ch.get("properties", [])}
            pv_name = ch.get("name", "")

            # Use the 'device' property as the canonical prefix when available.
            device_prop = props.get("device", "")

            # Fallback: derive stem from PV name  (PREFIX:FIELD -> PREFIX)
            parts = pv_name.split(":")
            if len(parts) >= 2:
                device_stem = parts[-2]
                pv_prefix = device_prop or ":".join(parts[:-1])
            else:
                device_stem = pv_name
                pv_prefix = device_prop or pv_name

            if device_stem not in devices:
                devices[device_stem] = {
                    "name": device_stem,
                    "devgroup": props.get("devgroup", ""),
                    "devtype": props.get("devtype", ""),
                    "prefix": pv_prefix,
                    "iocname": props.get("iocName", ""),
                    "template": props.get("template", ""),
                    "properties": props,
                    "pvs": [],
                }
            devices[device_stem]["pvs"].append(pv_name)

        return list(devices.values())


def _props_dict(channel: Dict[str, Any]) -> Dict[str, str]:
    """Flatten channel properties list to a simple dict."""
    return {p["name"]: p.get("value", "") for p in channel.get("properties", [])}

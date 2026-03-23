"""
Optional Ophyd device factory — creates Ophyd device instances from YAML config.

This module is only functional when ``ophyd`` and ``infn_ophyd_hal`` are installed.
If they are missing, ``create_ophyd_devices`` returns an empty dict gracefully.
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

try:
    from infn_ophyd_hal.device_factory import DeviceFactory

    OPHYD_AVAILABLE = True
except ImportError:
    OPHYD_AVAILABLE = False


def create_ophyd_devices(beamline_values: Dict[str, Any]) -> Dict[str, object]:
    """Create Ophyd device instances from beamline configuration.

    Args:
        beamline_values: Parsed values.yaml dict containing epicsConfiguration.iocs.

    Returns:
        Dictionary of device_name -> ophyd_device. Empty if ophyd is not available.
    """
    if not OPHYD_AVAILABLE:
        logger.info("ophyd / infn_ophyd_hal not installed — skipping device creation")
        return {}

    factory = DeviceFactory()
    devices: Dict[str, object] = {}

    epics_config = beamline_values.get("epicsConfiguration", {})
    iocs = epics_config.get("iocs", [])

    for ioc_config in iocs:
        ioc_name = ioc_config.get("name")
        if not ioc_name or ioc_config.get("disable", False):
            continue

        devgroup = ioc_config.get("devgroup")
        devtype = ioc_config.get("devtype")
        if not devgroup:
            continue

        ioc_prefix = ioc_config.get("iocprefix", "")
        iocname = ioc_config.get("name", "")

        try:
            device_list = ioc_config.get("devices", [])
            if device_list:
                for device_config in device_list:
                    device_name = device_config.get("name")
                    if not device_name:
                        continue
                    if "iocroot" in ioc_config:
                        pv_prefix = f"{ioc_prefix}:{ioc_config['iocroot']}:{device_name}"
                    else:
                        pv_prefix = f"{ioc_prefix}:{device_name}"

                    myconfig = ioc_config.copy()
                    myconfig["iocname"] = iocname
                    myconfig.update(device_config)
                    ophyd_device = factory.create_device(
                        devgroup=devgroup,
                        devtype=devtype,
                        prefix=pv_prefix,
                        name=device_name,
                        config=myconfig,
                    )
                    if ophyd_device:
                        key = device_name
                        if key in devices:
                            key = f"{ioc_name}_{device_name}"
                        devices[key] = ophyd_device
                        logger.info(f"Created Ophyd device: {key} (prefix={pv_prefix})")
            else:
                pv_prefix = f"{ioc_prefix}"
                ophyd_device = factory.create_device(
                    devgroup=devgroup,
                    devtype=devtype,
                    prefix=pv_prefix,
                    name=ioc_name,
                    config=ioc_config,
                )
                if ophyd_device:
                    devices[ioc_name] = ophyd_device
                    logger.info(f"Created Ophyd device: {ioc_name} ({devgroup}/{devtype})")
        except Exception as e:
            logger.error(f"Failed to create Ophyd device for {ioc_name}: {e}", exc_info=True)

    logger.info(f"Created {len(devices)} Ophyd devices")
    return devices

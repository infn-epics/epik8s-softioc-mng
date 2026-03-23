"""Optional Ophyd integration — only loaded when ophyd and infn_ophyd_hal are available."""

from iocmng.ophyd.factory import create_ophyd_devices

__all__ = ["create_ophyd_devices"]

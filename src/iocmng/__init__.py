"""
iocmng - A pluggable task/job framework for IOC Manager applications.

Provides base classes for tasks (continuous) and jobs (one-shot) that can be
dynamically loaded into a running IOC Manager process via REST API.
"""

__version__ = "2.4.0"

from iocmng.base.task import TaskBase
from iocmng.base.job import JobBase
from iocmng.core import pv_client
from iocmng.runner import run_ioc

__all__ = ["TaskBase", "JobBase", "pv_client", "run_ioc", "__version__"]

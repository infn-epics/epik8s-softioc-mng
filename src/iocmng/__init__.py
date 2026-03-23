"""
iocmng - A pluggable task/job framework for IOC Manager applications.

Provides base classes for tasks (continuous) and jobs (one-shot) that can be
dynamically loaded into a running IOC Manager process via REST API.
"""

__version__ = "2.0.0"

from iocmng.base.task import TaskBase
from iocmng.base.job import JobBase

__all__ = ["TaskBase", "JobBase", "__version__"]

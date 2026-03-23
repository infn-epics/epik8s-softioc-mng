"""
Example Job — demonstrates how to create a one-shot job.

This file can live in its own git repository. When added via the REST API,
the IOC Manager will clone the repo, validate this module, and make the job
available for execution.
"""

from iocmng import JobBase
from iocmng.base.job import JobResult


class ExampleDiagnostics(JobBase):
    """A simple diagnostics job that collects system info."""

    def initialize(self):
        self.logger.info("ExampleDiagnostics initialized")

    def execute(self) -> JobResult:
        import platform

        info = {
            "python_version": platform.python_version(),
            "system": platform.system(),
            "machine": platform.machine(),
            "node": platform.node(),
        }
        return JobResult(success=True, data=info, message="Diagnostics collected")

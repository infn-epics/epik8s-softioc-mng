"""
Example Task — demonstrates how to create a continuous monitoring task.

This file can live in its own git repository. When added via the REST API,
the IOC Manager will clone the repo, validate this module, and start the task.
"""

import random
from iocmng import TaskBase


class ExampleMonitor(TaskBase):
    """A simple continuous monitoring task that generates random values."""

    def initialize(self):
        self.logger.info("ExampleMonitor initialized")
        self.value = 0.0

    def execute(self):
        self.value = random.uniform(0, 100)
        self.logger.debug(f"Current value: {self.value}")
        self.set_pv("OUTPUT_RESULT", self.value)

    def cleanup(self):
        self.logger.info("ExampleMonitor cleaned up")

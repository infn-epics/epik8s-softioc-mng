"""
Example standalone soft IOC application.

This demonstrates how to use ``iocmng`` purely as a library to create a
soft IOC process without the REST API server.

Run directly::

    python examples/standalone_ioc.py

Or via the CLI helper::

    iocmng-run -m examples.standalone_ioc --config examples/example_task_config.yaml --prefix MY:IOC
"""

import math
import time

from iocmng import TaskBase, run_ioc


class SineWaveIOC(TaskBase):
    """A minimal soft IOC that publishes a sine wave."""

    def initialize(self):
        self.t0 = time.time()
        self.frequency = self.parameters.get("frequency", 1.0)
        self.amplitude = self.parameters.get("amplitude", 10.0)
        self.logger.info(
            "SineWaveIOC ready — freq=%.2f Hz, amp=%.2f",
            self.frequency,
            self.amplitude,
        )

    def execute(self):
        elapsed = time.time() - self.t0
        value = self.amplitude * math.sin(2 * math.pi * self.frequency * elapsed)
        self.set_output("SINE", value)

    def cleanup(self):
        self.logger.info("SineWaveIOC stopped")


if __name__ == "__main__":
    run_ioc(
        SineWaveIOC,
        config={
            "prefix": "SINE",
            "parameters": {
                "mode": "continuous",
                "interval": 0.1,
                "frequency": 0.5,
                "amplitude": 10.0,
            },
            "arguments": {
                "outputs": {
                    "SINE": {
                        "type": "float",
                        "value": 0.0,
                        "unit": "arb",
                        "low": -10,
                        "high": 10,
                    },
                },
            },
        },
        prefix="TEST",
        name="sine-wave",
    )

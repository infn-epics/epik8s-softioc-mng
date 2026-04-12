"""Built-in declarative task — behaviour fully driven by config rules.

Use this class when the entire plugin logic can be expressed
declaratively via wired inputs/outputs, rules, and rule_defaults in
``config.yaml``.  No Python code is required.

Example start.sh.j2::

    iocmng-run \\
      --module iocmng.declarative \\
      --class-name DeclarativeTask \\
      --config config.yaml \\
      --prefix {{iocprefix}} \\
      --name {{iocname}} \\
      ...
"""

from iocmng.base.task import TaskBase


class DeclarativeTask(TaskBase):
    """A task with no user code — behaviour is fully driven by config rules."""

    def initialize(self):
        pass

    def execute(self):
        pass

    def cleanup(self):
        pass

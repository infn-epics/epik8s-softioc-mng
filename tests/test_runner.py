"""Tests for the standalone runner module.

All softioc interactions are mocked so no EPICS infrastructure is needed.
"""

import signal
import textwrap
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

from iocmng.base.job import JobBase, JobResult
from iocmng.base.task import TaskBase
from iocmng.runner import _resolve_class, run_ioc


# ---------------------------------------------------------------------------
# Fixtures — concrete plugin classes for testing
# ---------------------------------------------------------------------------

class DummyTask(TaskBase):
    """Minimal concrete task."""

    def initialize(self):
        pass

    def execute(self):
        pass

    def cleanup(self):
        pass


class DummyJob(JobBase):
    """Minimal concrete job."""

    def initialize(self):
        pass

    def execute(self):
        return JobResult(success=True, data={"key": 1}, message="ok")


# ---------------------------------------------------------------------------
# _resolve_class
# ---------------------------------------------------------------------------

class TestResolveClass:

    def test_auto_detect_task(self, tmp_path):
        """Auto-detect first TaskBase subclass from a module."""
        code = textwrap.dedent("""\
            from iocmng import TaskBase

            class FoundTask(TaskBase):
                def initialize(self): pass
                def execute(self): pass
                def cleanup(self): pass
        """)
        mod = types.ModuleType("_test_mod")
        exec(compile(code, "<test>", "exec"), mod.__dict__)
        import sys
        sys.modules["_test_mod"] = mod
        try:
            cls = _resolve_class("_test_mod")
            assert cls.__name__ == "FoundTask"
        finally:
            del sys.modules["_test_mod"]

    def test_explicit_class_name(self):
        import sys
        mod = types.ModuleType("_test_mod2")
        mod.DummyTask = DummyTask
        sys.modules["_test_mod2"] = mod
        try:
            cls = _resolve_class("_test_mod2", "DummyTask")
            assert cls is DummyTask
        finally:
            del sys.modules["_test_mod2"]

    def test_class_not_found_raises(self):
        import sys
        mod = types.ModuleType("_test_mod3")
        sys.modules["_test_mod3"] = mod
        try:
            with pytest.raises(ValueError, match="not found"):
                _resolve_class("_test_mod3", "Nope")
        finally:
            del sys.modules["_test_mod3"]

    def test_no_subclass_raises(self):
        import sys
        mod = types.ModuleType("_test_mod4")
        mod.x = 42
        sys.modules["_test_mod4"] = mod
        try:
            with pytest.raises(ValueError, match="No TaskBase/JobBase"):
                _resolve_class("_test_mod4")
        finally:
            del sys.modules["_test_mod4"]


# ---------------------------------------------------------------------------
# run_ioc — task path
# ---------------------------------------------------------------------------

class TestRunIocTask:

    @patch("iocmng.runner._init_softioc")
    def test_task_lifecycle(self, mock_init_ioc, tmp_path):
        """run_ioc for a task calls _init_softioc, initialize, start, stop."""
        config = {
            "prefix": "TEST",
            "parameters": {"mode": "continuous", "interval": 0.01},
            "arguments": {
                "outputs": {"VAL": {"type": "float", "value": 0.0}},
            },
        }
        config_path = tmp_path / "config.yaml"
        config_path.write_text(yaml.dump(config))

        # We need run_ioc to not block forever.  Simulate shutdown after a
        # brief delay by flipping the _shutdown sentinel.
        import iocmng.runner as runner_mod

        def _fire_shutdown():
            import time
            time.sleep(0.15)
            runner_mod._shutdown = True

        t = threading.Thread(target=_fire_shutdown, daemon=True)
        t.start()

        run_ioc(
            DummyTask,
            config=str(config_path),
            prefix="TEST",
            pva=True,
            name="test-task",
        )

        mock_init_ioc.assert_called_once()
        t.join(timeout=2)

    @patch("iocmng.runner._init_softioc")
    def test_explicit_prefix_used_as_full_pv_prefix(self, mock_init_ioc):
        """--prefix is the complete PV prefix; plugin_prefix must NOT be appended."""
        import iocmng.runner as runner_mod

        def _fire():
            import time
            time.sleep(0.1)
            runner_mod._shutdown = True

        t = threading.Thread(target=_fire, daemon=True)
        t.start()

        run_ioc(
            DummyTask,
            config={"parameters": {"interval": 0.01}},
            prefix="SPARC:SOFTINTLK",
            pva=False,
            name="softinterlock",
        )
        t.join(timeout=2)

        # pv_prefix must equal the explicit prefix, not SPARC:SOFTINTLK:SOFTINTERLOCK
        captured = mock_init_ioc.call_args[0][0]
        assert captured.pv_prefix == "SPARC:SOFTINTLK"

    @patch("iocmng.runner._init_softioc")
    def test_task_with_dict_config(self, mock_init_ioc):
        """run_ioc accepts a dict as config."""
        import iocmng.runner as runner_mod

        def _fire():
            import time
            time.sleep(0.1)
            runner_mod._shutdown = True

        t = threading.Thread(target=_fire, daemon=True)
        t.start()

        run_ioc(
            DummyTask,
            config={"parameters": {"interval": 0.01}},
            pva=False,
            name="dict-task",
        )

        t.join(timeout=2)
        from iocmng.core import pv_client
        assert pv_client.get_provider() == "ca"


# ---------------------------------------------------------------------------
# run_ioc — job path
# ---------------------------------------------------------------------------

class TestRunIocJob:

    @patch("iocmng.runner._init_softioc")
    def test_job_runs_once_then_blocks(self, mock_init_ioc):
        """run_ioc for a job calls run() once, then blocks until shutdown."""
        import iocmng.runner as runner_mod

        def _fire():
            import time
            time.sleep(0.15)
            runner_mod._shutdown = True

        t = threading.Thread(target=_fire, daemon=True)
        t.start()

        with patch.object(DummyJob, "run", return_value=JobResult(success=True, message="ok")) as mock_run:
            run_ioc(DummyJob, name="test-job")

        mock_run.assert_called_once()
        mock_init_ioc.assert_called_once()
        t.join(timeout=2)


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------

class TestCli:

    def test_main_missing_module_exits(self):
        """Calling main() without --module should fail."""
        import sys
        with patch.object(sys, "argv", ["iocmng-run"]):
            with pytest.raises(SystemExit):
                from iocmng.runner import main
                main()

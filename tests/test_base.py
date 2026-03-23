"""Tests for the iocmng base classes."""

import pytest
import time
from pathlib import Path

from iocmng.base.task import TaskBase
from iocmng.base.job import JobBase, JobResult


# ------------------------------------------------------------------
# Concrete test implementations
# ------------------------------------------------------------------


class DummyTask(TaskBase):
    def initialize(self):
        self.init_called = True

    def execute(self):
        self.exec_count = getattr(self, "exec_count", 0) + 1

    def cleanup(self):
        self.cleanup_called = True


class DummyJob(JobBase):
    def initialize(self):
        self.init_called = True

    def execute(self) -> JobResult:
        return JobResult(success=True, data={"answer": 42}, message="OK")


class IncompleteTask(TaskBase):
    """Missing execute and cleanup — should fail validation."""

    def initialize(self):
        pass


# ------------------------------------------------------------------
# Task tests
# ------------------------------------------------------------------


class TestTaskBase:
    def test_create_task(self):
        t = DummyTask(name="test_task")
        assert t.name == "test_task"
        assert t.mode == "continuous"
        assert t._iocmng_type == "task"

    def test_task_lifecycle(self):
        t = DummyTask(name="lifecycle", parameters={"interval": 0.1})
        t.initialize()
        assert t.init_called
        t.start()
        time.sleep(0.5)
        assert t.running
        assert t.exec_count > 0
        t.stop()
        assert not t.running

    def test_task_is_abstract(self):
        with pytest.raises(TypeError):
            IncompleteTask(name="bad")

    def test_pv_prefix_default(self):
        t = DummyTask(name="mytest", beamline_config={"beamline": "sparc", "namespace": "ns1"})
        assert t.pv_prefix == "SPARC:NS1:MYTEST"

    def test_pv_prefix_override(self):
        t = DummyTask(name="mytest", prefix="CUSTOM:PREFIX")
        assert t.pv_prefix == "CUSTOM:PREFIX:MYTEST"


# ------------------------------------------------------------------
# Job tests
# ------------------------------------------------------------------


class TestJobBase:
    def test_create_job(self):
        j = DummyJob(name="test_job")
        assert j.name == "test_job"
        assert j._iocmng_type == "job"

    def test_job_pv_prefix_default(self):
        j = DummyJob(name="myjob", beamline_config={"beamline": "sparc", "namespace": "ns1"})
        assert j.pv_prefix == "SPARC:NS1:MYJOB"

    def test_job_pv_prefix_override(self):
        j = DummyJob(name="myjob", prefix="CUSTOM:PREFIX")
        assert j.pv_prefix == "CUSTOM:PREFIX:MYJOB"

    def test_run_job(self):
        j = DummyJob(name="runner")
        result = j.run()
        assert result.success
        assert result.data == {"answer": 42}
        assert j.last_result is result

    def test_job_failure(self):
        class FailJob(JobBase):
            def initialize(self):
                pass

            def execute(self):
                raise RuntimeError("boom")

        j = FailJob(name="fail")
        result = j.run()
        assert not result.success
        assert "boom" in result.message

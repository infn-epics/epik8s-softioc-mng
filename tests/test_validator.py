"""Tests for the plugin validator."""

import textwrap
from pathlib import Path

import pytest

from iocmng.core.validator import PluginValidator


@pytest.fixture
def tmp_plugin(tmp_path):
    """Create a temporary plugin file."""

    def _create(code: str, filename: str = "my_plugin.py"):
        p = tmp_path / filename
        p.write_text(textwrap.dedent(code))
        return p

    return _create


class TestPluginValidator:
    def test_valid_task(self, tmp_plugin):
        code = """\
        from iocmng import TaskBase

        class MyTask(TaskBase):
            def initialize(self):
                pass
            def execute(self):
                pass
            def cleanup(self):
                pass
        """
        path = tmp_plugin(code)
        result = PluginValidator.validate_module_path(path)
        assert result.ok
        assert result.class_name == "MyTask"
        assert result.plugin_type == "task"

    def test_valid_job(self, tmp_plugin):
        code = """\
        from iocmng import JobBase
        from iocmng.base.job import JobResult

        class MyJob(JobBase):
            def initialize(self):
                pass
            def execute(self):
                return JobResult(success=True, message="done")
        """
        path = tmp_plugin(code)
        result = PluginValidator.validate_module_path(path)
        assert result.ok
        assert result.class_name == "MyJob"
        assert result.plugin_type == "job"

    def test_syntax_error(self, tmp_plugin):
        code = """\
        def broken(
            pass  # missing closing paren
        """
        path = tmp_plugin(code)
        result = PluginValidator.validate_module_path(path)
        assert not result.ok
        assert any("Syntax error" in e for e in result.errors)

    def test_no_base_class(self, tmp_plugin):
        code = """\
        class NotAPlugin:
            pass
        """
        path = tmp_plugin(code)
        result = PluginValidator.validate_module_path(path)
        assert not result.ok
        assert any("No class found" in e for e in result.errors)

    def test_abstract_methods_missing(self, tmp_plugin):
        code = """\
        from iocmng import TaskBase

        class Incomplete(TaskBase):
            def initialize(self):
                pass
            # missing execute and cleanup
        """
        path = tmp_plugin(code)
        result = PluginValidator.validate_module_path(path)
        assert not result.ok
        assert any("Abstract methods" in e for e in result.errors)

    def test_file_not_found(self):
        result = PluginValidator.validate_module_path(Path("/nonexistent.py"))
        assert not result.ok

    def test_validate_directory(self, tmp_path):
        code = textwrap.dedent("""\
        from iocmng import TaskBase

        class DirTask(TaskBase):
            def initialize(self): pass
            def execute(self): pass
            def cleanup(self): pass
        """)
        (tmp_path / "plugin.py").write_text(code)
        result = PluginValidator.validate_directory(tmp_path)
        assert result.ok
        assert result.class_name == "DirTask"

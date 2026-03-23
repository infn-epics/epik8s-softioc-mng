"""Tests for the plugin loader — path support and config.yaml loading."""

import textwrap
from pathlib import Path

import pytest
import yaml

from iocmng.core.loader import PluginLoader


@pytest.fixture
def loader(tmp_path):
    return PluginLoader(plugins_dir=tmp_path)


class TestPluginSourcePath:
    def test_no_path(self, loader, tmp_path):
        assert loader.plugin_source_path("myplugin") == tmp_path / "myplugin"

    def test_with_path(self, loader, tmp_path):
        assert loader.plugin_source_path("myplugin", "src/task") == tmp_path / "myplugin" / "src" / "task"


class TestLoadPluginConfig:
    def _create_plugin(self, loader, name, path="", config=None):
        """Helper — create a fake plugin directory with optional config.yaml."""
        source = loader.plugin_source_path(name, path)
        source.mkdir(parents=True, exist_ok=True)
        if config is not None:
            (source / "config.yaml").write_text(yaml.dump(config))
        return source

    def test_no_config(self, loader):
        self._create_plugin(loader, "p1")
        cfg = loader.load_plugin_config("p1")
        assert cfg == {}

    def test_load_config(self, loader):
        expected = {
            "parameters": {"threshold": 10},
            "pvs": {"outputs": {"VALUE": {"type": "float", "value": 0}}},
        }
        self._create_plugin(loader, "p2", config=expected)
        cfg = loader.load_plugin_config("p2")
        assert cfg["parameters"]["threshold"] == 10
        assert "VALUE" in cfg["pvs"]["outputs"]

    def test_load_config_with_path(self, loader):
        expected = {"parameters": {"mode": "triggered"}}
        self._create_plugin(loader, "p3", path="sub/dir", config=expected)
        cfg = loader.load_plugin_config("p3", path="sub/dir")
        assert cfg["parameters"]["mode"] == "triggered"

    def test_config_yml_fallback(self, loader):
        """config.yml should also be accepted."""
        source = loader.plugin_source_path("p4")
        source.mkdir(parents=True)
        (source / "config.yml").write_text(yaml.dump({"parameters": {"x": 1}}))
        cfg = loader.load_plugin_config("p4")
        assert cfg["parameters"]["x"] == 1


class TestInstallRequirements:
    def _make_plugin(self, loader, name, path="", add_req_in_path=False, add_req_in_root=False):
        root = loader.plugin_path(name)
        source = loader.plugin_source_path(name, path)
        root.mkdir(parents=True, exist_ok=True)
        source.mkdir(parents=True, exist_ok=True)
        if add_req_in_path:
            (source / "requirements.txt").write_text("# empty\n")
        if add_req_in_root:
            (root / "requirements.txt").write_text("# root empty\n")
        return root, source

    def test_no_requirements(self, loader):
        self._make_plugin(loader, "nr")
        ok, msg = loader.install_requirements("nr")
        assert ok
        assert "No requirements" in msg

    def test_requirements_in_path_preferred(self, loader):
        self._make_plugin(loader, "rpref", path="sub", add_req_in_path=True, add_req_in_root=True)
        # Just verify it picks up the file without error
        ok, msg = loader.install_requirements("rpref", path="sub")
        assert ok


class TestValidateWithPath:
    def test_validate_missing_path(self, loader):
        # Plugin root exists but sub-path doesn't
        loader.plugin_path("vp").mkdir()
        result = loader.validate("vp", path="nonexistent")
        assert not result.ok

    def test_validate_with_path(self, loader):
        source = loader.plugin_source_path("vp2", "src")
        source.mkdir(parents=True)
        code = textwrap.dedent("""\
        from iocmng import TaskBase

        class MyTask(TaskBase):
            def initialize(self): pass
            def execute(self): pass
            def cleanup(self): pass
        """)
        (source / "plugin.py").write_text(code)
        result = loader.validate("vp2", path="src")
        assert result.ok

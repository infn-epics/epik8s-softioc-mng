"""Controller tests for autostart persistence and ordering."""

from pathlib import Path

from iocmng.core.controller import IocMngController, PluginInfo


class TestAutostartPersistence:
    def test_registry_roundtrip(self, tmp_path: Path):
        ctrl = IocMngController(plugins_dir=tmp_path / "plugins", disable_ophyd=True)

        info = PluginInfo(
            name="p1",
            git_url="https://example.invalid/repo.git",
            plugin_type="task",
            class_name="MyTask",
            path="task",
            branch="main",
            pat=None,
            parameters={"x": 1},
            start_parameters={"x": 1, "y": 2},
            pv_definitions={"outputs": {"VALUE": {"type": "float", "value": 0.0}}},
            auto_start=True,
            auto_start_on_boot=True,
            autostart_order=10,
        )

        ctrl._upsert_autostart_registry_entry(info)
        loaded = ctrl.load_persisted_autostart_plugins()

        assert len(loaded) == 1
        assert loaded[0]["name"] == "p1"
        assert loaded[0]["auto_start_on_boot"] is True
        assert loaded[0]["autostart_order"] == 10

        ctrl._remove_autostart_registry_entry("p1")
        loaded2 = ctrl.load_persisted_autostart_plugins()
        assert loaded2 == []


class TestAutostartOrdering:
    def test_add_plugins_from_config_orders_by_autostart_order(self, tmp_path: Path):
        ctrl = IocMngController(plugins_dir=tmp_path / "plugins", disable_ophyd=True)

        called = []

        def fake_add_plugin(**kwargs):
            called.append(kwargs["name"])
            return True, "ok", None

        ctrl.add_plugin = fake_add_plugin  # type: ignore[method-assign]

        plugins = [
            {"name": "third", "git_url": "u3", "autostart_order": 30},
            {"name": "first", "git_url": "u1", "autostart_order": 10},
            {"name": "second", "git_url": "u2", "autostart_order": 20},
            {"name": "unordered", "git_url": "u4"},
        ]

        results = ctrl.add_plugins_from_config(plugins)

        assert [r["name"] for r in results] == ["first", "second", "third", "unordered"]
        assert called == ["first", "second", "third", "unordered"]

"""Tests for the wired input (link) and declarative rule engine."""

import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest
import yaml

from iocmng.base.task import TaskBase
from iocmng.core.plugin_spec import (
    PvArgumentSpec,
    PluginSpec,
    RuleSpec,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class LinkTask(TaskBase):
    """Minimal task for testing link engine."""

    def initialize(self):
        self.execute_count = 0
        self.changed_events = []

    def execute(self):
        self.execute_count += 1

    def cleanup(self):
        pass

    def on_input_changed(self, key, value, old_value):
        self.changed_events.append((key, value, old_value))


# ---------------------------------------------------------------------------
# PvArgumentSpec link fields
# ---------------------------------------------------------------------------

class TestPvArgumentSpecLinks:

    def test_unwired_by_default(self):
        spec = PvArgumentSpec.from_config("X", "input", {"type": "int", "value": 0})
        assert spec.wired is False
        assert spec.link is None
        assert spec.link_mode == "poll"
        assert spec.trigger is False

    def test_wired_input(self):
        spec = PvArgumentSpec.from_config("llrf1", "input", {
            "type": "int",
            "value": 0,
            "link": "SPARC:LLRF1:app:rf_ctrl",
            "link_mode": "monitor",
            "poll_rate": 2.0,
            "trigger": True,
        })
        assert spec.wired is True
        assert spec.link == "SPARC:LLRF1:app:rf_ctrl"
        assert spec.link_mode == "monitor"
        assert spec.poll_rate == 2.0
        assert spec.trigger is True

    def test_to_dict_includes_link(self):
        spec = PvArgumentSpec.from_config("a", "input", {
            "type": "int", "value": 0,
            "link": "PV:A", "trigger": True,
        })
        d = spec.to_dict()
        assert d["link"] == "PV:A"
        assert d["trigger"] is True

    def test_to_dict_no_link_when_unwired(self):
        spec = PvArgumentSpec.from_config("a", "input", {"type": "int", "value": 0})
        d = spec.to_dict()
        assert "link" not in d

    def test_mode_alias(self):
        """'mode' is accepted as alias for 'link_mode'."""
        spec = PvArgumentSpec.from_config("x", "input", {
            "type": "int", "value": 0,
            "link": "EXT:PV", "mode": "monitor",
        })
        assert spec.link_mode == "monitor"


# ---------------------------------------------------------------------------
# RuleSpec
# ---------------------------------------------------------------------------

class TestRuleSpec:

    def test_from_config(self):
        rule = RuleSpec.from_config({
            "id": "R1",
            "condition": "a == 0 and b == 1",
            "message": "test fired",
            "actuators": {"a": 1},
            "outputs": {"STATUS": 4},
        })
        assert rule.id == "R1"
        assert rule.condition == "a == 0 and b == 1"
        assert rule.actuators == {"a": 1}
        assert rule.outputs == {"STATUS": 4}

    def test_to_dict(self):
        rule = RuleSpec(id="R1", condition="x == 0", message="msg", actuators={"x": 1})
        d = rule.to_dict()
        assert d["id"] == "R1"
        assert d["condition"] == "x == 0"
        assert d["actuators"] == {"x": 1}

    def test_defaults(self):
        rule = RuleSpec.from_config({"id": "R", "condition": "True"})
        assert rule.message == ""
        assert rule.actuators == {}
        assert rule.outputs == {}


# ---------------------------------------------------------------------------
# PluginSpec rules parsing
# ---------------------------------------------------------------------------

class TestPluginSpecRules:

    def test_no_rules_by_default(self):
        spec = PluginSpec.from_config({"parameters": {}})
        assert spec.rules == []

    def test_rules_parsed(self):
        config = {
            "rules": [
                {"id": "R1", "condition": "x == 0", "message": "hi"},
                {"id": "R2", "condition": "y > 1"},
            ],
        }
        spec = PluginSpec.from_config(config)
        assert len(spec.rules) == 2
        assert spec.rules[0].id == "R1"
        assert spec.rules[1].condition == "y > 1"


# ---------------------------------------------------------------------------
# Link polling in continuous mode
# ---------------------------------------------------------------------------

class TestLinkPolling:

    @patch("iocmng.core.pv_client.get")
    @patch("iocmng.core.pv_client.init")
    def test_poll_links_reads_wired_inputs(self, mock_init, mock_get):
        """_poll_links should call pv_client.get for wired poll inputs."""
        mock_get.return_value = 42
        config = {
            "parameters": {"interval": 0.01},
            "arguments": {
                "inputs": {
                    "sensor": {
                        "type": "int", "value": 0,
                        "link": "EXT:SENSOR:VAL",
                    },
                    "setpoint": {"type": "float", "value": 0.0},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()
        task._poll_links()

        mock_get.assert_called_once_with("EXT:SENSOR:VAL", timeout=5.0)
        assert task.link_values["sensor"] == 42

    @patch("iocmng.core.pv_client.get")
    @patch("iocmng.core.pv_client.init")
    def test_poll_links_trigger_fires_on_change(self, mock_init, mock_get):
        """on_input_changed should fire when a trigger input changes."""
        mock_get.return_value = 99
        config = {
            "parameters": {"interval": 0.01},
            "arguments": {
                "inputs": {
                    "val": {
                        "type": "int", "value": 0,
                        "link": "EXT:VAL", "trigger": True,
                    },
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()
        task.link_values["val"] = 0  # old value

        task._poll_links()
        assert len(task.changed_events) == 1
        assert task.changed_events[0] == ("val", 99, 0)

    @patch("iocmng.core.pv_client.get")
    @patch("iocmng.core.pv_client.init")
    def test_poll_links_no_trigger_when_not_set(self, mock_init, mock_get):
        """on_input_changed should NOT fire when trigger=false."""
        mock_get.return_value = 99
        config = {
            "parameters": {"interval": 0.01},
            "arguments": {
                "inputs": {
                    "val": {
                        "type": "int", "value": 0,
                        "link": "EXT:VAL", "trigger": False,
                    },
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()
        task.link_values["val"] = 0

        task._poll_links()
        assert task.changed_events == []

    @patch("iocmng.core.pv_client.get")
    @patch("iocmng.core.pv_client.init")
    def test_poll_rate_gating(self, mock_init, mock_get):
        """Inputs with poll_rate should skip reads if not enough time elapsed."""
        mock_get.return_value = 1
        config = {
            "parameters": {"interval": 0.01},
            "arguments": {
                "inputs": {
                    "slow": {
                        "type": "int", "value": 0,
                        "link": "EXT:SLOW", "poll_rate": 100.0,
                    },
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        # First poll always reads
        task._poll_links()
        assert mock_get.call_count == 1

        # Second poll within poll_rate should skip
        task._poll_links()
        assert mock_get.call_count == 1


# ---------------------------------------------------------------------------
# Link put
# ---------------------------------------------------------------------------

class TestLinkPut:

    @patch("iocmng.core.pv_client.put")
    @patch("iocmng.core.pv_client.init")
    def test_link_put_calls_pv_client(self, mock_init, mock_put):
        config = {
            "arguments": {
                "inputs": {
                    "act": {
                        "type": "int", "value": 0,
                        "link": "EXT:ACTUATOR",
                    },
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        task.link_put("act", 0)
        mock_put.assert_called_once_with("EXT:ACTUATOR", 0, timeout=5.0)

    def test_link_put_unwired_raises(self):
        config = {
            "arguments": {
                "inputs": {
                    "local": {"type": "int", "value": 0},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        with pytest.raises(KeyError, match="not a wired PV"):
            task.link_put("local", 0)


# ---------------------------------------------------------------------------
# Output links
# ---------------------------------------------------------------------------

class TestOutputLinks:

    def test_output_wired(self):
        """Outputs can have link fields just like inputs."""
        spec = PvArgumentSpec.from_config("alarm", "output", {
            "type": "bool", "value": 0,
            "link": "EXT:ALARM:STATUS",
        })
        assert spec.wired is True
        assert spec.link == "EXT:ALARM:STATUS"
        assert spec.direction == "output"

    @patch("iocmng.core.pv_client.put")
    @patch("iocmng.core.pv_client.init")
    def test_set_pv_forwards_to_linked_output(self, mock_init, mock_put):
        """set_pv on a wired output should auto-forward to the external PV."""
        config = {
            "arguments": {
                "outputs": {
                    "alarm": {
                        "type": "bool", "value": 0,
                        "link": "EXT:ALARM:STATUS",
                    },
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        mock_pv = MagicMock()
        task.pvs["alarm"] = mock_pv

        task.set_pv("alarm", 1)

        mock_pv.set.assert_called_once_with(1)
        mock_put.assert_called_once_with("EXT:ALARM:STATUS", 1, timeout=5.0)

    @patch("iocmng.core.pv_client.put")
    @patch("iocmng.core.pv_client.init")
    def test_set_pv_no_forward_for_unwired_output(self, mock_init, mock_put):
        """set_pv on an unwired output should NOT call pv_client.put."""
        config = {
            "arguments": {
                "outputs": {
                    "local_out": {"type": "int", "value": 0},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        mock_pv = MagicMock()
        task.pvs["local_out"] = mock_pv

        task.set_pv("local_out", 42)
        mock_pv.set.assert_called_once_with(42)
        mock_put.assert_not_called()

    @patch("iocmng.core.pv_client.put")
    @patch("iocmng.core.pv_client.init")
    def test_link_put_works_on_wired_output(self, mock_init, mock_put):
        """link_put should work on wired outputs, not just inputs."""
        config = {
            "arguments": {
                "outputs": {
                    "cmd": {
                        "type": "int", "value": 0,
                        "link": "EXT:CMD",
                    },
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        task.link_put("cmd", 5)
        mock_put.assert_called_once_with("EXT:CMD", 5, timeout=5.0)

    @patch("iocmng.core.pv_client.get")
    @patch("iocmng.core.pv_client.init")
    def test_poll_links_reads_wired_outputs(self, mock_init, mock_get):
        """Wired outputs with poll mode should be read for read-back."""
        mock_get.return_value = 99
        config = {
            "parameters": {"interval": 0.01},
            "arguments": {
                "outputs": {
                    "readback": {
                        "type": "int", "value": 0,
                        "link": "EXT:READBACK",
                    },
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()
        task.link_values["readback"] = 0

        task._poll_links()
        mock_get.assert_called_once()
        assert task.link_values["readback"] == 99

    @patch("iocmng.core.pv_client.monitor")
    @patch("iocmng.core.pv_client.init")
    def test_start_link_monitors_includes_outputs(self, mock_init, mock_monitor):
        """Wired outputs with monitor mode should get subscriptions."""
        config = {
            "arguments": {
                "outputs": {
                    "fb": {
                        "type": "int", "value": 0,
                        "link": "EXT:FEEDBACK", "link_mode": "monitor",
                    },
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        task._start_link_monitors()

        mock_monitor.assert_called_once()
        assert mock_monitor.call_args.kwargs["name"] == "_link_fb"

    @patch("iocmng.core.pv_client.put")
    @patch("iocmng.core.pv_client.init")
    def test_rule_fires_output_with_link(self, mock_init, mock_put):
        """When a rule sets a wired output, it should forward to the external PV."""
        config = {
            "arguments": {
                "inputs": {
                    "sensor": {"type": "int", "value": 0, "link": "EXT:SENSOR"},
                },
                "outputs": {
                    "alarm": {
                        "type": "bool", "value": 0,
                        "link": "EXT:ALARM:ACTIVE",
                    },
                },
            },
            "rules": [
                {
                    "id": "ALARM_ON",
                    "condition": "sensor == 0",
                    "message": "Sensor down — alarm on",
                    "outputs": {"alarm": 1},
                },
            ],
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        mock_pv = MagicMock()
        task.pvs["alarm"] = mock_pv

        task.link_values = {"sensor": 0}
        task._evaluate_rules()

        # Local PV was set
        mock_pv.set.assert_called_with(1)
        # External PV was also forwarded
        mock_put.assert_called_once_with("EXT:ALARM:ACTIVE", 1, timeout=5.0)


# ---------------------------------------------------------------------------
# Declarative rule evaluation
# ---------------------------------------------------------------------------

class TestRuleEvaluation:

    @patch("iocmng.core.pv_client.put")
    @patch("iocmng.core.pv_client.init")
    def test_rule_fires_actuator(self, mock_init, mock_put):
        config = {
            "parameters": {"timeout": 2.0},
            "arguments": {
                "inputs": {
                    "sensor": {"type": "int", "value": 0, "link": "EXT:SENSOR"},
                    "valve": {"type": "int", "value": 1, "link": "EXT:VALVE"},
                },
            },
            "rules": [
                {
                    "id": "CLOSE_VALVE",
                    "condition": "sensor == 0 and valve == 1",
                    "message": "Sensor down — closing valve",
                    "actuators": {"valve": 0},
                },
            ],
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        task.link_values = {"sensor": 0, "valve": 1}
        task._evaluate_rules()

        mock_put.assert_called_once_with("EXT:VALVE", 0, timeout=2.0)

    @patch("iocmng.core.pv_client.put")
    @patch("iocmng.core.pv_client.init")
    def test_rule_does_not_fire_when_condition_false(self, mock_init, mock_put):
        config = {
            "arguments": {
                "inputs": {
                    "sensor": {"type": "int", "value": 0, "link": "EXT:SENSOR"},
                },
            },
            "rules": [
                {"id": "R1", "condition": "sensor == 0", "actuators": {}},
            ],
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        task.link_values = {"sensor": 1}  # condition is false
        task._evaluate_rules()

        mock_put.assert_not_called()

    @patch("iocmng.core.pv_client.put")
    @patch("iocmng.core.pv_client.init")
    def test_rule_sets_outputs(self, mock_init, mock_put):
        config = {
            "arguments": {
                "inputs": {
                    "x": {"type": "int", "value": 0, "link": "EXT:X"},
                },
                "outputs": {
                    "ALARM": {"type": "bool", "value": 0},
                },
            },
            "rules": [
                {"id": "R1", "condition": "x == 0", "outputs": {"ALARM": 1}},
            ],
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        # Mock the PV
        mock_pv = MagicMock()
        task.pvs["ALARM"] = mock_pv

        task.link_values = {"x": 0}
        task._evaluate_rules()

        mock_pv.set.assert_called_with(1)

    def test_no_rules_is_noop(self):
        spec = PluginSpec.from_config({"parameters": {}})
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()
        task._evaluate_rules()  # Should not raise


# ---------------------------------------------------------------------------
# Reactive mode
# ---------------------------------------------------------------------------

class TestReactiveMode:

    def test_mode_accepted(self):
        spec = PluginSpec.from_config({"parameters": {"mode": "reactive"}})
        task = LinkTask(name="test", plugin_spec=spec)
        assert task.mode == "reactive"

    @patch("iocmng.core.pv_client.monitor")
    @patch("iocmng.core.pv_client.init")
    def test_start_link_monitors_for_monitor_mode(self, mock_init, mock_monitor):
        config = {
            "parameters": {"mode": "reactive"},
            "arguments": {
                "inputs": {
                    "sig": {
                        "type": "int", "value": 0,
                        "link": "EXT:SIG", "link_mode": "monitor",
                    },
                    "local": {"type": "int", "value": 0},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        task._start_link_monitors()

        mock_monitor.assert_called_once()
        assert mock_monitor.call_args.kwargs["name"] == "_link_sig"

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


# ---------------------------------------------------------------------------
# Rule defaults
# ---------------------------------------------------------------------------

class TestRuleDefaults:

    @patch("iocmng.core.pv_client.put")
    @patch("iocmng.core.pv_client.init")
    def test_rule_defaults_applied_before_rules(self, mock_init, mock_put):
        """rule_defaults should reset outputs before rule evaluation."""
        config = {
            "arguments": {
                "inputs": {
                    "sensor": {"type": "int", "value": 0, "link": "EXT:S"},
                },
                "outputs": {
                    "ALARM": {"type": "bool", "value": 0},
                },
            },
            "rule_defaults": {"ALARM": 0},
            "rules": [
                {"id": "R1", "condition": "sensor == 0", "outputs": {"ALARM": 1}},
            ],
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        mock_pv = MagicMock()
        task.pvs["ALARM"] = mock_pv

        # sensor == 0 → rule fires → ALARM set to 0 (default) then 1 (rule)
        task.link_values = {"sensor": 0}
        task._evaluate_rules()
        calls = mock_pv.set.call_args_list
        assert calls[0] == call(0)  # rule_defaults
        assert calls[1] == call(1)  # rule output

    @patch("iocmng.core.pv_client.put")
    @patch("iocmng.core.pv_client.init")
    def test_rule_defaults_remain_when_no_rule_fires(self, mock_init, mock_put):
        """When no rule fires, only rule_defaults should be applied."""
        config = {
            "arguments": {
                "inputs": {
                    "sensor": {"type": "int", "value": 0, "link": "EXT:S"},
                },
                "outputs": {
                    "ALARM": {"type": "bool", "value": 0},
                },
            },
            "rule_defaults": {"ALARM": 0},
            "rules": [
                {"id": "R1", "condition": "sensor == 0", "outputs": {"ALARM": 1}},
            ],
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        mock_pv = MagicMock()
        task.pvs["ALARM"] = mock_pv

        task.link_values = {"sensor": 1}  # condition false
        task._evaluate_rules()

        mock_pv.set.assert_called_once_with(0)  # only default

    def test_plugin_spec_parses_rule_defaults(self):
        config = {
            "rule_defaults": {"INTLK_ACT": 0, "MOVING": 0},
            "rules": [{"id": "R", "condition": "True"}],
        }
        spec = PluginSpec.from_config(config)
        assert spec.rule_defaults == {"INTLK_ACT": 0, "MOVING": 0}

    def test_no_rule_defaults_by_default(self):
        spec = PluginSpec.from_config({"parameters": {}})
        assert spec.rule_defaults == {}


# ---------------------------------------------------------------------------
# Message PV
# ---------------------------------------------------------------------------

class TestMessagePv:

    def test_rule_spec_message_pv(self):
        rule = RuleSpec.from_config({
            "id": "R1",
            "condition": "True",
            "message": "something happened",
            "message_pv": "INTLK_MSG",
        })
        assert rule.message_pv == "INTLK_MSG"

    def test_rule_spec_message_pv_default_none(self):
        rule = RuleSpec.from_config({"id": "R", "condition": "True"})
        assert rule.message_pv is None

    def test_rule_spec_to_dict_includes_message_pv(self):
        rule = RuleSpec(id="R", condition="True", message="msg", message_pv="OUT")
        d = rule.to_dict()
        assert d["message_pv"] == "OUT"

    @patch("iocmng.core.pv_client.put")
    @patch("iocmng.core.pv_client.init")
    def test_fire_rule_writes_timestamped_message(self, mock_init, mock_put):
        config = {
            "arguments": {
                "inputs": {
                    "x": {"type": "int", "value": 0, "link": "EXT:X"},
                },
                "outputs": {
                    "MSG": {"type": "string", "value": ""},
                },
            },
            "rules": [
                {
                    "id": "R1",
                    "condition": "x == 0",
                    "message": "Alert!",
                    "message_pv": "MSG",
                },
            ],
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        mock_pv = MagicMock()
        task.pvs["MSG"] = mock_pv
        task.link_values = {"x": 0}

        task._evaluate_rules()

        # Should have been called with a timestamped string
        assert mock_pv.set.called
        written_msg = mock_pv.set.call_args[0][0]
        assert "Alert!" in written_msg
        assert " - " in written_msg  # timestamp separator


# ---------------------------------------------------------------------------
# DeclarativeTask
# ---------------------------------------------------------------------------

class TestDeclarativeTask:

    def test_declarative_task_is_taskbase(self):
        from iocmng.declarative import DeclarativeTask
        assert issubclass(DeclarativeTask, TaskBase)

    @patch("iocmng.core.pv_client.put")
    @patch("iocmng.core.pv_client.init")
    def test_declarative_task_runs_rules(self, mock_init, mock_put):
        """DeclarativeTask should evaluate rules via _run_wrapper flow."""
        from iocmng.declarative import DeclarativeTask
        config = {
            "parameters": {"interval": 0.01},
            "arguments": {
                "inputs": {
                    "a": {"type": "int", "value": 0, "link": "EXT:A"},
                },
            },
            "rule_defaults": {},
            "rules": [
                {"id": "R1", "condition": "a == 0", "actuators": {"a": 1}},
            ],
        }
        spec = PluginSpec.from_config(config)
        task = DeclarativeTask(name="test", plugin_spec=spec)
        task.initialize()

        task.link_values = {"a": 0}
        task._evaluate_rules()

        mock_put.assert_called_once()


# ---------------------------------------------------------------------------
# Buffer size
# ---------------------------------------------------------------------------

class TestBufferSize:

    def test_buffer_size_parsed(self):
        spec = PvArgumentSpec.from_config("sig", "input", {
            "type": "float", "value": 0.0, "link": "EXT:SIG",
            "buffer_size": 100,
        })
        assert spec.buffer_size == 100

    def test_buffer_size_none_by_default(self):
        spec = PvArgumentSpec.from_config("sig", "input", {
            "type": "float", "value": 0.0,
        })
        assert spec.buffer_size is None

    def test_buffer_size_zero_is_none(self):
        spec = PvArgumentSpec.from_config("sig", "input", {
            "type": "float", "value": 0.0, "buffer_size": 0,
        })
        assert spec.buffer_size is None

    @patch("iocmng.core.pv_client.init")
    def test_init_buffers(self, mock_init):
        config = {
            "parameters": {},
            "arguments": {
                "inputs": {
                    "sig": {"type": "float", "value": 0.0, "link": "EXT:SIG", "buffer_size": 10},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()
        assert "sig" in task._link_buffers
        assert task._link_buffers["sig"].maxlen == 10

    @patch("iocmng.core.pv_client.init")
    def test_buffer_append(self, mock_init):
        config = {
            "parameters": {},
            "arguments": {
                "inputs": {
                    "sig": {"type": "float", "value": 0.0, "link": "EXT:SIG", "buffer_size": 5},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        for i in range(7):
            task._buffer_append("sig", float(i))

        buf = list(task._link_buffers["sig"])
        assert buf == [2.0, 3.0, 4.0, 5.0, 6.0]

    @patch("iocmng.core.pv_client.init")
    def test_build_eval_context_includes_buffers(self, mock_init):
        config = {
            "parameters": {"threshold": 5.0},
            "arguments": {
                "inputs": {
                    "sig": {"type": "float", "value": 0.0, "link": "EXT:SIG", "buffer_size": 10},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        task.link_values["sig"] = 3.0
        task._buffer_append("sig", 1.0)
        task._buffer_append("sig", 2.0)
        task._buffer_append("sig", 3.0)

        ctx = task._build_eval_context()
        assert ctx["sig"] == 3.0
        assert ctx["sig_buf"] == [1.0, 2.0, 3.0]
        assert ctx["threshold"] == 5.0


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------

class TestTransformSpec:

    def test_transform_parsed(self):
        from iocmng.core.plugin_spec import TransformSpec
        t = TransformSpec.from_config({"output": "avg", "expression": "mean(sig_buf)"})
        assert t.output == "avg"
        assert t.expression == "mean(sig_buf)"

    def test_transform_to_dict(self):
        from iocmng.core.plugin_spec import TransformSpec
        t = TransformSpec(output="avg", expression="mean(sig_buf)")
        d = t.to_dict()
        assert d == {"output": "avg", "expression": "mean(sig_buf)"}


class TestTransformEvaluation:

    @patch("iocmng.core.pv_client.init")
    def test_evaluate_transforms_sets_output(self, mock_init):
        config = {
            "parameters": {},
            "arguments": {
                "inputs": {
                    "sig": {"type": "float", "value": 0.0, "link": "EXT:SIG", "buffer_size": 10},
                },
                "outputs": {
                    "avg": {"type": "float", "value": 0.0},
                },
            },
            "transforms": [
                {"output": "avg", "expression": "mean(sig_buf)"},
            ],
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        mock_pv = MagicMock()
        task.pvs["avg"] = mock_pv

        for v in [1.0, 2.0, 3.0]:
            task._buffer_append("sig", v)

        task._evaluate_transforms()

        mock_pv.set.assert_called_once_with(2.0)

    @patch("iocmng.core.pv_client.init")
    def test_transforms_chained(self, mock_init):
        """Later transforms can reference outputs of earlier transforms."""
        config = {
            "parameters": {},
            "arguments": {
                "inputs": {
                    "x": {"type": "float", "value": 0.0, "link": "EXT:X"},
                },
                "outputs": {
                    "doubled": {"type": "float", "value": 0.0},
                    "tripled": {"type": "float", "value": 0.0},
                },
            },
            "transforms": [
                {"output": "doubled", "expression": "x * 2"},
                {"output": "tripled", "expression": "doubled + x"},
            ],
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()
        task.link_values["x"] = 5.0

        mock_doubled = MagicMock()
        mock_tripled = MagicMock()
        task.pvs["doubled"] = mock_doubled
        task.pvs["tripled"] = mock_tripled

        task._evaluate_transforms()

        mock_doubled.set.assert_called_once_with(10.0)
        mock_tripled.set.assert_called_once_with(15.0)

    @patch("iocmng.core.pv_client.init")
    def test_transform_with_function(self, mock_init):
        config = {
            "parameters": {},
            "arguments": {
                "inputs": {
                    "sig": {"type": "float", "value": 0.0, "link": "EXT:SIG", "buffer_size": 100},
                },
                "outputs": {
                    "noise": {"type": "float", "value": 0.0},
                },
            },
            "transforms": [
                {"output": "noise", "expression": "std(sig_buf)"},
            ],
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        mock_pv = MagicMock()
        task.pvs["noise"] = mock_pv

        for v in [1.0, 1.0, 1.0]:
            task._buffer_append("sig", v)

        task._evaluate_transforms()
        mock_pv.set.assert_called_once_with(0.0)

    @patch("iocmng.core.pv_client.init")
    def test_rules_see_eval_context(self, mock_init):
        """Rules should have access to buffers and parameters."""
        config = {
            "parameters": {"threshold": 2.0},
            "arguments": {
                "inputs": {
                    "sig": {"type": "float", "value": 0.0, "link": "EXT:SIG", "buffer_size": 10},
                },
                "outputs": {
                    "alarm": {"type": "int", "value": 0},
                },
            },
            "rules": [
                {"id": "R1", "condition": "mean(sig_buf) > threshold", "outputs": {"alarm": 1}},
            ],
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        mock_pv = MagicMock()
        task.pvs["alarm"] = mock_pv

        for v in [3.0, 3.0, 3.0]:
            task._buffer_append("sig", v)
        task.link_values["sig"] = 3.0

        task._evaluate_rules()

        mock_pv.set.assert_called_once_with(1)


# ---------------------------------------------------------------------------
# Connection tracking
# ---------------------------------------------------------------------------

class TestConnectionTracking:

    def test_wired_input_names_populated(self):
        """_wired_input_names should list all wired input names in order."""
        config = {
            "arguments": {
                "inputs": {
                    "a": {"type": "int", "value": 0, "link": "EXT:A"},
                    "b": {"type": "int", "value": 0},           # not wired
                    "c": {"type": "int", "value": 0, "link": "EXT:C"},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        assert task._wired_input_names == ["a", "c"]
        assert task._wired_output_names == []

    def test_wired_output_names_populated(self):
        config = {
            "arguments": {
                "outputs": {
                    "x": {"type": "int", "value": 0, "link": "EXT:X"},
                    "y": {"type": "int", "value": 0},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        assert task._wired_output_names == ["x"]
        assert task._wired_input_names == []

    @patch("iocmng.core.pv_client.get")
    @patch("iocmng.core.pv_client.init")
    def test_initial_check_sets_connected(self, mock_init, mock_get):
        """_initial_connectivity_check should mark reachable PVs as connected."""
        mock_get.return_value = 42
        config = {
            "arguments": {
                "inputs": {
                    "a": {"type": "int", "value": 0, "link": "EXT:A"},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()
        task._initial_connectivity_check()
        assert task._link_connected["a"] is True
        assert task.link_values["a"] == 42

    @patch("iocmng.core.pv_client.get")
    @patch("iocmng.core.pv_client.init")
    def test_initial_check_sets_disconnected(self, mock_init, mock_get):
        """_initial_connectivity_check should mark unreachable PVs as disconnected."""
        mock_get.side_effect = TimeoutError("timeout")
        config = {
            "arguments": {
                "inputs": {
                    "a": {"type": "int", "value": 0, "link": "EXT:A"},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()
        task._initial_connectivity_check()
        assert task._link_connected["a"] is False
        assert task.link_values.get("a", 0) == 0  # stays at default

    @patch("iocmng.core.pv_client.get")
    @patch("iocmng.core.pv_client.init")
    def test_initial_check_updates_conn_pv(self, mock_init, mock_get):
        """CONN_INP array PV should reflect initial connectivity."""
        def _get_side_effect(pv, **kw):
            if pv == "EXT:A":
                return 1
            raise TimeoutError("timeout")

        mock_get.side_effect = _get_side_effect
        config = {
            "arguments": {
                "inputs": {
                    "a": {"type": "int", "value": 0, "link": "EXT:A"},
                    "b": {"type": "int", "value": 0, "link": "EXT:B"},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        mock_conn_pv = MagicMock()
        task.pvs["CONN_INP"] = mock_conn_pv

        task._initial_connectivity_check()

        # a connected (1), b disconnected (0)
        mock_conn_pv.set.assert_called_with([1, 0])

    def test_make_conn_callback_updates_state(self):
        """Connection callback should update _link_connected and call _update_conn_pv."""
        config = {
            "arguments": {
                "inputs": {
                    "sensor": {"type": "int", "value": 0, "link": "EXT:S", "link_mode": "monitor"},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()
        task._link_connected["sensor"] = True

        mock_conn_pv = MagicMock()
        task.pvs["CONN_INP"] = mock_conn_pv

        sensor_spec = spec.inputs["sensor"]
        cb = task._make_conn_callback("sensor", sensor_spec)

        # Simulate disconnect
        cb(False)
        assert task._link_connected["sensor"] is False
        mock_conn_pv.set.assert_called_with([0])

        # Simulate reconnect
        cb(True)
        assert task._link_connected["sensor"] is True
        mock_conn_pv.set.assert_called_with([1])

    @patch("iocmng.core.pv_client.get")
    @patch("iocmng.core.pv_client.init")
    def test_poll_links_tracks_connection_loss(self, mock_init, mock_get):
        """_poll_links should mark PV as disconnected when get() fails."""
        call_count = [0]

        def _side_effect(pv, **kw):
            call_count[0] += 1
            if call_count[0] <= 1:
                return 42  # first call succeeds
            raise TimeoutError("timeout")  # subsequent calls fail

        mock_get.side_effect = _side_effect
        config = {
            "parameters": {"interval": 0.01},
            "arguments": {
                "inputs": {
                    "val": {"type": "int", "value": 0, "link": "EXT:VAL"},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        mock_conn_pv = MagicMock()
        task.pvs["CONN_INP"] = mock_conn_pv

        # First poll — connected
        task._poll_links()
        assert task._link_connected["val"] is True

        # Second poll — disconnected
        task._poll_links()
        assert task._link_connected["val"] is False
        mock_conn_pv.set.assert_called_with([0])

    @patch("iocmng.core.pv_client.get")
    @patch("iocmng.core.pv_client.init")
    def test_poll_links_tracks_reconnection(self, mock_init, mock_get):
        """_poll_links should mark PV as connected again after recovery."""
        call_count = [0]

        def _side_effect(pv, **kw):
            call_count[0] += 1
            if call_count[0] == 2:
                raise TimeoutError("timeout")  # second call fails
            return 42

        mock_get.side_effect = _side_effect
        config = {
            "parameters": {"interval": 0.01},
            "arguments": {
                "inputs": {
                    "val": {"type": "int", "value": 0, "link": "EXT:VAL"},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        mock_conn_pv = MagicMock()
        task.pvs["CONN_INP"] = mock_conn_pv

        task._poll_links()   # ok
        task._poll_links()   # fail
        assert task._link_connected["val"] is False
        task._poll_links()   # ok again
        assert task._link_connected["val"] is True

    @patch("iocmng.core.pv_client.monitor")
    @patch("iocmng.core.pv_client.init")
    def test_start_link_monitors_passes_conn_callback(self, mock_init, mock_monitor):
        """_start_link_monitors should pass conn_callback to pv_client.monitor."""
        config = {
            "arguments": {
                "inputs": {
                    "sig": {
                        "type": "int", "value": 0,
                        "link": "EXT:SIG", "link_mode": "monitor",
                    },
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()

        task._start_link_monitors()

        mock_monitor.assert_called_once()
        call_kwargs = mock_monitor.call_args
        assert call_kwargs.kwargs.get("conn_callback") is not None


class TestEnableMonitorToggle:

    @patch("iocmng.core.pv_client.unmonitor")
    @patch("iocmng.core.pv_client.monitor")
    @patch("iocmng.core.pv_client.init")
    def test_enable_toggle_stops_and_restarts_monitors(self, mock_init, mock_monitor, mock_unmonitor):
        """ENABLE=0 should unmonitor; ENABLE=1 should monitor again."""
        config = {
            "parameters": {"mode": "continuous"},
            "arguments": {
                "inputs": {
                    "sig": {
                        "type": "int", "value": 0,
                        "link": "EXT:SIG", "link_mode": "monitor",
                    },
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()
        task.running = True

        task._start_link_monitors()
        assert mock_monitor.call_count == 1
        assert task._link_monitors_active is True

        task._on_enable_changed(0)
        mock_unmonitor.assert_called_once_with("_link_sig")
        assert task._link_monitors_active is False

        task._on_enable_changed(1)
        assert mock_monitor.call_count == 2
        assert task._link_monitors_active is True

    def test_run_wrapper_pauses_when_disabled_and_resumes(self):
        """Continuous loop should pause on disable and continue after re-enable."""
        config = {
            "parameters": {"interval": 0.01, "mode": "continuous"},
            "arguments": {
                "inputs": {
                    "x": {"type": "int", "value": 0},
                },
            },
        }
        spec = PluginSpec.from_config(config)
        task = LinkTask(name="test", plugin_spec=spec)
        task.initialize()
        task.running = True
        task.enabled = False

        poll_calls = {"n": 0}

        def _sleep(_interval):
            if not task.enabled:
                task.enabled = True
            else:
                task.running = False

        with patch.object(task, "_poll_links", side_effect=lambda: poll_calls.__setitem__("n", poll_calls["n"] + 1)), \
             patch.object(task, "_evaluate_transforms"), \
             patch.object(task, "_evaluate_rules"), \
             patch.object(task, "execute"), \
             patch.object(task, "step_cycle"), \
             patch("iocmng.base.task.time.sleep", side_effect=_sleep):
            task._run_wrapper()

        assert poll_calls["n"] == 1

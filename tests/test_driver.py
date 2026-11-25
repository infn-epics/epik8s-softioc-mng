"""Tests for the driver source: PVs read from and written to a device driver."""

import math
import sys
import threading
import time
import types
from unittest.mock import MagicMock

import pytest

from iocmng.base.task import TaskBase
from iocmng.core.driver import DriverEngine, SimulatedDriver
from iocmng.core.plugin_spec import PluginSpec, PvArgumentSpec
from iocmng.core.validator import PluginValidator


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeDriver:
    """Records calls; fails on demand."""

    def __init__(self, values=None):
        self.values = dict(values or {})
        self.calls = []
        self.fail_connect = 0     # number of connect() calls to fail
        self.fail_read = False
        self.fail_write = False
        self.lock = threading.Lock()

    def connect(self):
        self.calls.append(("connect",))
        if self.fail_connect:
            self.fail_connect -= 1
            raise ConnectionError("no route to device")

    def close(self):
        self.calls.append(("close",))

    def read(self, names):
        self.calls.append(("read", tuple(names)))
        if self.fail_read:
            raise TimeoutError("device timeout")
        return {n: self.values.get(n) for n in names}

    def write(self, name, value):
        self.calls.append(("write", name, value))
        if self.fail_write:
            raise ValueError("value refused")
        self.values[name] = value

    def command(self, name):
        self.calls.append(("command", name))


class DeviceTask(TaskBase):
    def initialize(self):
        pass

    def execute(self):
        pass

    def cleanup(self):
        pass


GAUGE_CONFIG = {
    "parameters": {"interval": 0.02},
    "arguments": {
        "inputs": {
            "Pressure": {"type": "float", "value": 1e-6, "source": "driver", "buffer_size": 5},
            "Unit": {"type": "enum", "source": "driver", "access": "rw", "choices": ["mbar", "Torr", "Pa"]},
            "Trace": {"type": "waveform", "source": "driver", "length": 8},
            "Reset": {"type": "bool", "source": "driver", "access": "cmd"},
        },
        "outputs": {
            "Alarm": {"type": "bool", "value": 0},
        },
    },
    "rule_defaults": {"Alarm": 0},
    "rules": [{"id": "HIGH", "condition": "Pressure > 1e-3", "outputs": {"Alarm": 1}}],
}


# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------

class TestDriverSpec:

    def test_driver_fields(self):
        spec = PluginSpec.from_config(GAUGE_CONFIG)
        p, u, t, r = (spec.inputs[n] for n in ("Pressure", "Unit", "Trace", "Reset"))
        assert p.driven and p.access == "ro" and not p.writable and not p.wired
        assert u.driven and u.writable and u.choices == ("mbar", "Torr", "Pa")
        assert t.type == "waveform" and t.length == 8 and not t.writable
        assert r.access == "cmd" and r.writable

    def test_unwired_input_still_writable(self):
        spec = PvArgumentSpec.from_config("SP", "input", {"type": "float"})
        assert spec.writable and not spec.driven

    def test_to_dict_roundtrip(self):
        spec = PluginSpec.from_config(GAUGE_CONFIG)
        d = spec.inputs["Unit"].to_dict()
        assert d["source"] == "driver" and d["access"] == "rw" and d["choices"] == ["mbar", "Torr", "Pa"]
        again = PvArgumentSpec.from_config("Unit", "input", d)
        for field in ("type", "source", "access", "choices", "writable", "driven"):
            assert getattr(again, field) == getattr(spec.inputs["Unit"], field)

    @pytest.mark.parametrize("cfg, message", [
        ({"type": "float", "source": "plc"}, "source must be"),
        ({"type": "float", "source": "driver", "link": "X:Y"}, "either a link or a source"),
        ({"type": "float", "access": "rw"}, "applies to driver PVs"),
        ({"type": "float", "source": "driver", "access": "cmd"}, "cmd PV is a bool"),
        ({"type": "enum", "choices": ["only"]}, "2..16 choices"),
        ({"type": "enum", "choices": ["a", "x" * 26]}, "at most 25"),
        ({"type": "waveform"}, "needs length"),
        ({"type": "waveform", "length": 4, "source": "driver", "access": "rw"}, "read-only"),
    ])
    def test_invalid(self, cfg, message):
        with pytest.raises(ValueError, match=message):
            PvArgumentSpec.from_config("X", "input", cfg)

    def test_validator_reports_field_errors(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            "arguments:\n  inputs:\n    U:\n      type: enum\n      source: driver\n      choices: [a]\n"
        )
        result = PluginValidator.validate_config_path(tmp_path)
        assert not result.ok
        assert any("2..16 choices" in e for e in result.errors)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def make_engine(driver, names=("A",), period=0.02, retry=0.05):
    events = {"values": [], "connection": [], "errors": []}
    engine = DriverEngine(
        driver, list(names), period,
        on_values=events["values"].append,
        on_connection=events["connection"].append,
        on_error=lambda exc, ctx: events["errors"].append((ctx, str(exc))),
        retry=retry,
    )
    return engine, events


class TestDriverEngine:

    def test_connects_and_polls(self):
        driver = FakeDriver({"A": 1.5})
        engine, ev = make_engine(driver)
        engine.start()
        try:
            assert wait_for(lambda: len(ev["values"]) >= 3)
        finally:
            engine.stop()
        assert ev["values"][0] == {"A": 1.5}
        assert ev["connection"] == [True, False]

    def test_write_and_command(self):
        driver = FakeDriver({"A": 0})
        engine, ev = make_engine(driver, period=10.0)   # no polling in the way
        done = threading.Event()
        engine.start()
        try:
            assert wait_for(lambda: engine.connected)
            engine.submit_write("A", 7)
            engine.submit_command("Reset", after=done.set)
            assert done.wait(2.0)
        finally:
            engine.stop()
        assert ("write", "A", 7) in driver.calls
        assert ("command", "Reset") in driver.calls
        assert driver.calls.index(("write", "A", 7)) < driver.calls.index(("command", "Reset"))

    def test_write_failure_keeps_connection(self):
        driver = FakeDriver({"A": 0})
        driver.fail_write = True
        engine, ev = make_engine(driver, period=10.0)
        engine.start()
        try:
            assert wait_for(lambda: engine.connected)
            engine.submit_write("A", 1)
            assert wait_for(lambda: ev["errors"])
            assert engine.connected
        finally:
            engine.stop()
        assert ev["errors"][0][0] == "write A failed"

    def test_read_failure_reconnects(self):
        driver = FakeDriver({"A": 2})
        engine, ev = make_engine(driver)
        engine.start()
        try:
            assert wait_for(lambda: ev["values"])
            driver.fail_read = True
            assert wait_for(lambda: False in ev["connection"])
            driver.fail_read = False
            assert wait_for(lambda: ev["connection"].count(True) >= 2)
        finally:
            engine.stop()
        assert ("device communication failed", "device timeout") in ev["errors"]
        assert ("close",) in driver.calls

    def test_connect_retry(self):
        driver = FakeDriver({"A": 3})
        driver.fail_connect = 2
        engine, ev = make_engine(driver)
        engine.start()
        try:
            assert wait_for(lambda: ev["values"])
        finally:
            engine.stop()
        assert [c for c in driver.calls if c == ("connect",)] == [("connect",)] * 3
        assert [e[0] for e in ev["errors"]] == ["connect failed"] * 2

    def test_command_while_disconnected_is_dropped_and_completed(self):
        driver = FakeDriver()
        driver.fail_connect = 1000
        engine, ev = make_engine(driver, retry=0.5)
        done = threading.Event()
        engine.start()
        try:
            assert wait_for(lambda: ev["errors"])
            engine.submit_command("Reset", after=done.set)
            assert done.wait(2.0)
        finally:
            engine.stop()
        assert ("command", "Reset") not in driver.calls
        assert any("not connected" in e[1] for e in ev["errors"])


# ---------------------------------------------------------------------------
# Task integration
# ---------------------------------------------------------------------------

def fake_records(task):
    """Stand-ins for softioc records: remember the last value set."""
    for name in list(task._driver_specs) + ["Alarm", "MESSAGE", "CONNECTED"]:
        rec = MagicMock()
        rec.value = None

        def _set(value, process=True, _rec=rec):
            _rec.value = value
        rec.set.side_effect = _set
        task.pvs[name] = rec


class TestTaskDriver:

    def make_task(self, driver, **params):
        config = dict(GAUGE_CONFIG)
        config["parameters"] = dict(GAUGE_CONFIG["parameters"], **params)
        task = DeviceTask(name="gauge", plugin_spec=PluginSpec.from_config(config))
        task.create_driver = lambda: driver
        fake_records(task)
        return task

    def test_values_published(self):
        driver = FakeDriver({"Pressure": 2e-6, "Unit": "Torr", "Trace": [1, 2, 3]})
        task = self.make_task(driver)
        task._start_driver()
        try:
            assert wait_for(lambda: "Trace" in task.link_values)
        finally:
            task._stop_driver()
        assert task.pvs["Pressure"].value == pytest.approx(2e-6)
        assert task.pvs["Unit"].value == 1                     # enum string -> index
        trace = task.pvs["Trace"].value
        assert len(trace) == 8 and list(trace[:3]) == [1, 2, 3] and math.isnan(trace[7])
        assert task.pvs["CONNECTED"].value == 0               # stopped -> disconnected
        assert "Reset" not in driver.calls[1][1]              # cmd PVs are never read
        # the readback of a setting is marked so its echo is not written back
        assert task._driver_echo["Unit"] == 1

    def test_put_and_command_reach_driver(self):
        driver = FakeDriver({"Pressure": 1e-6, "Unit": 0})
        task = self.make_task(driver, poll_period=10.0)
        put_unit = task._make_driver_put("Unit", task._driver_specs["Unit"])
        put_reset = task._make_driver_put("Reset", task._driver_specs["Reset"])
        put_unit(2)                                           # before start: ignored
        task._start_driver()
        try:
            assert wait_for(lambda: task._driver_engine.connected)
            put_unit(2)
            put_reset(1)
            assert wait_for(lambda: ("command", "Reset") in driver.calls)
            assert wait_for(lambda: task.pvs["Reset"].value == 0)   # back to Idle
        finally:
            task._stop_driver()
        assert [c for c in driver.calls if c[0] == "write"] == [("write", "Unit", 2)]

    def test_readback_echo_not_written_back(self):
        driver = FakeDriver({"Pressure": 1e-6, "Unit": 1})
        task = self.make_task(driver)
        put_unit = task._make_driver_put("Unit", task._driver_specs["Unit"])
        task._start_driver()
        try:
            assert wait_for(lambda: task.link_values.get("Unit") == 1)
            put_unit(1)                                       # softioc echo of the readback
            put_unit(2)                                       # a real client write
            assert wait_for(lambda: ("write", "Unit", 2) in driver.calls)
        finally:
            task._stop_driver()
        assert [c for c in driver.calls if c[0] == "write"] == [("write", "Unit", 2)]

    def test_rules_see_configured_values_before_first_read(self):
        driver = FakeDriver()
        driver.fail_connect = 1000
        task = self.make_task(driver)
        task._start_driver()
        try:
            task._evaluate_rules()                            # no NameError
        finally:
            task._stop_driver()
        assert task.link_values["Pressure"] == pytest.approx(1e-6)
        assert task.pvs["Alarm"].value == 0

    def test_rules_see_driver_values(self):
        driver = FakeDriver({"Pressure": 5e-3, "Unit": 0})
        task = self.make_task(driver)
        task._start_driver()
        try:
            assert wait_for(lambda: "Pressure" in task.link_values)
            task._evaluate_rules()
        finally:
            task._stop_driver()
        assert task.pvs["Alarm"].value == 1
        assert task._link_buffers["Pressure"]

    def test_reactive_mode_evaluates_on_change(self):
        driver = FakeDriver({"Pressure": 1e-6, "Unit": 0})
        task = self.make_task(driver, mode="reactive")
        task._start_driver()
        try:
            assert wait_for(lambda: task.pvs["Alarm"].value == 0)   # first read evaluated
            driver.values["Pressure"] = 1.0
            assert wait_for(lambda: task.pvs["Alarm"].value == 1)
        finally:
            task._stop_driver()

    def test_error_logged_once(self, caplog):
        driver = FakeDriver({"Pressure": 1e-6})
        driver.fail_connect = 1000
        task = self.make_task(driver, reconnect_period=0.01)
        with caplog.at_level("ERROR"):
            task._start_driver()
            try:
                assert wait_for(lambda: len([c for c in driver.calls if c == ("connect",)]) >= 5)
            finally:
                task._stop_driver()
        assert len([r for r in caplog.records if "connect failed" in r.getMessage()]) == 1
        assert "connect failed" in task.pvs["MESSAGE"].value

    def test_hooks(self):
        events = []
        driver = FakeDriver({"Pressure": 1e-6})
        driver.fail_connect = 1
        task = self.make_task(driver, reconnect_period=0.01)
        task.on_driver_connection = lambda connected: events.append(("connection", connected))
        task.on_driver_error = lambda message: events.append(("error", message))
        task.on_driver_recovered = lambda: events.append(("recovered",))
        task._start_driver()
        try:
            assert wait_for(lambda: ("recovered",) in events)
        finally:
            task._stop_driver()
        assert [e[0] for e in events[:3]] == ["error", "connection", "recovered"]
        assert "connect failed" in events[0][1]
        assert events[-1] == ("connection", False)

    def test_failing_hook_is_logged(self, caplog):
        task = self.make_task(FakeDriver())

        def boom(connected):
            raise RuntimeError("boom")
        boom.__name__ = "on_driver_connection"
        task.on_driver_connection = boom
        with caplog.at_level("ERROR"):
            task._on_driver_connection(True)
        assert task.pvs["CONNECTED"].value == 1
        assert any("on_driver_connection failed" in r.getMessage() for r in caplog.records)


class TestCreateDriver:

    def test_simulate(self):
        task = DeviceTask(name="g", plugin_spec=PluginSpec.from_config(
            dict(GAUGE_CONFIG, parameters={"simulate": True})))
        driver = task.create_driver()
        assert isinstance(driver, SimulatedDriver)
        driver.connect()
        values = driver.read(["Pressure", "Unit", "Trace"])
        assert values["Pressure"] == pytest.approx(1e-6, abs=0.02)
        assert values["Unit"] == 0 and len(values["Trace"]) == 8

    def test_module_class(self, monkeypatch):
        module = types.ModuleType("fake_gauge_driver")

        class Gauge(FakeDriver):
            def __init__(self, host, port):
                super().__init__()
                self.host, self.port = host, port
        module.Gauge = Gauge
        monkeypatch.setitem(sys.modules, "fake_gauge_driver", module)
        task = DeviceTask(name="g", plugin_spec=PluginSpec.from_config(dict(
            GAUGE_CONFIG, parameters={"driver": "fake_gauge_driver:Gauge",
                                      "driver_args": {"host": "gauge01", "port": 8000}})))
        driver = task.create_driver()
        assert (driver.host, driver.port) == ("gauge01", 8000)

    def test_missing_driver(self):
        task = DeviceTask(name="g", plugin_spec=PluginSpec.from_config(GAUGE_CONFIG))
        with pytest.raises(ValueError, match="parameters.driver"):
            task.create_driver()

    def test_no_driver_pvs_no_engine(self):
        task = DeviceTask(name="g", plugin_spec=PluginSpec.from_config(
            {"arguments": {"inputs": {"SP": {"type": "float"}}}}))
        task._start_driver()
        assert task._driver_engine is None and task.driver is None

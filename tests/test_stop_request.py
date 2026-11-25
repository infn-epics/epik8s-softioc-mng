"""A stop request (SIGTERM) ends the initial connectivity check early."""

import signal
from unittest.mock import MagicMock, patch

from iocmng import runner
from iocmng.base.task import TaskBase
from iocmng.core.plugin_spec import PluginSpec


class LinkTask(TaskBase):
    def initialize(self):
        pass

    def execute(self):
        pass

    def cleanup(self):
        pass


def _task(n_inputs=3):
    inputs = {f"in{i}": {"type": "int", "value": 0, "link": f"EXT:IN{i}"} for i in range(n_inputs)}
    task = LinkTask(name="test", plugin_spec=PluginSpec.from_config({"arguments": {"inputs": inputs}}))
    task.initialize()
    return task


@patch("iocmng.core.pv_client.get")
@patch("iocmng.core.pv_client.init")
def test_stop_request_abandons_the_check(mock_init, mock_get):
    task = _task()

    def get_then_stop(pv, **kw):
        task.request_stop()          # SIGTERM arrives while the first PV is awaited
        raise TimeoutError(pv)

    mock_get.side_effect = get_then_stop
    task._initial_connectivity_check()

    assert mock_get.call_count == 1
    assert task.running is False


@patch("iocmng.core.pv_client.monitor")
@patch("iocmng.core.pv_client.get")
@patch("iocmng.core.pv_client.init")
def test_start_returns_after_a_stop_request(mock_init, mock_get, mock_monitor):
    task = _task()
    mock_get.side_effect = lambda pv, **kw: (task.request_stop(), 0)[1]

    task.start()

    assert mock_get.call_count == 1
    mock_monitor.assert_not_called()  # no link monitors, no task thread
    assert task._thread is None or not task._thread.is_alive()
    task.stop()


@patch("iocmng.core.pv_client.get", return_value=1)
@patch("iocmng.core.pv_client.init")
def test_check_runs_in_full_without_a_stop_request(mock_init, mock_get):
    task = _task()
    task._initial_connectivity_check()
    assert mock_get.call_count == 3


def test_signal_handler_forwards_to_the_task(monkeypatch):
    instance = MagicMock()
    monkeypatch.setattr(runner, "_instance", instance)
    monkeypatch.setattr(runner, "_shutdown", False)

    runner._signal_handler(signal.SIGTERM, None)

    assert runner._shutdown is True
    instance.request_stop.assert_called_once_with()


def test_signal_handler_without_a_task(monkeypatch):
    monkeypatch.setattr(runner, "_instance", object())  # a job: no request_stop
    monkeypatch.setattr(runner, "_shutdown", False)
    runner._signal_handler(signal.SIGTERM, None)
    assert runner._shutdown is True

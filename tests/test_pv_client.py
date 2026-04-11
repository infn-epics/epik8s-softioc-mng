"""Tests for the pv_client module and the /pvs REST endpoints.

All p4p interactions are mocked so no EPICS network is required.
"""

import types
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from iocmng.core import pv_client
from iocmng.api.app import create_app


# ---------------------------------------------------------------------------
# Unit tests for the pv_client module
# ---------------------------------------------------------------------------

class TestPvClientInit:
    """Test init / provider selection."""

    def setup_method(self):
        # Reset global state before each test.
        pv_client._provider = "pva"
        pv_client._context = None
        pv_client._subscriptions.clear()

    def test_default_provider_is_pva(self):
        pv_client.init(pva=True)
        assert pv_client.get_provider() == "pva"

    def test_switch_to_ca(self):
        pv_client.init(pva=False)
        assert pv_client.get_provider() == "ca"

    def test_init_resets_context(self):
        pv_client._context = "stale"
        pv_client.init(pva=True)
        assert pv_client._context is None


class TestPvClientGet:

    def setup_method(self):
        pv_client._provider = "pva"
        pv_client._context = None
        pv_client._subscriptions.clear()

    @patch("iocmng.core.pv_client._get_context")
    def test_get_returns_value(self, mock_ctx_fn):
        mock_ctx = MagicMock()
        mock_ctx.get.return_value = 42.0
        mock_ctx_fn.return_value = mock_ctx

        result = pv_client.get("TEST:PV", timeout=2.0)

        mock_ctx.get.assert_called_once_with("TEST:PV", timeout=2.0)
        assert result == 42.0

    @patch("iocmng.core.pv_client._get_context")
    def test_get_propagates_exception(self, mock_ctx_fn):
        mock_ctx = MagicMock()
        mock_ctx.get.side_effect = TimeoutError("no response")
        mock_ctx_fn.return_value = mock_ctx

        with pytest.raises(TimeoutError):
            pv_client.get("BAD:PV")


class TestPvClientPut:

    def setup_method(self):
        pv_client._provider = "pva"
        pv_client._context = None
        pv_client._subscriptions.clear()

    @patch("iocmng.core.pv_client._get_context")
    def test_put_calls_context(self, mock_ctx_fn):
        mock_ctx = MagicMock()
        mock_ctx_fn.return_value = mock_ctx

        pv_client.put("TEST:PV", 99, timeout=3.0)

        mock_ctx.put.assert_called_once_with("TEST:PV", 99, timeout=3.0)


class TestPvClientMonitor:

    def setup_method(self):
        pv_client._provider = "pva"
        pv_client._context = None
        pv_client._subscriptions.clear()

    @patch("iocmng.core.pv_client._get_context")
    def test_monitor_returns_key(self, mock_ctx_fn):
        mock_ctx = MagicMock()
        mock_sub = MagicMock()
        mock_ctx.monitor.return_value = mock_sub
        mock_ctx_fn.return_value = mock_ctx

        cb = lambda v: None
        key = pv_client.monitor("TEST:PV", cb, name="mykey")

        assert key == "mykey"
        mock_ctx.monitor.assert_called_once_with("TEST:PV", cb)
        assert "mykey" in pv_client._subscriptions

    @patch("iocmng.core.pv_client._get_context")
    def test_monitor_default_key_is_pv_name(self, mock_ctx_fn):
        mock_ctx = MagicMock()
        mock_ctx.monitor.return_value = MagicMock()
        mock_ctx_fn.return_value = mock_ctx

        key = pv_client.monitor("TEST:PV2", lambda v: None)
        assert key == "TEST:PV2"

    @patch("iocmng.core.pv_client._get_context")
    def test_unmonitor_closes_subscription(self, mock_ctx_fn):
        mock_ctx = MagicMock()
        mock_sub = MagicMock()
        mock_ctx.monitor.return_value = mock_sub
        mock_ctx_fn.return_value = mock_ctx

        key = pv_client.monitor("TEST:PV", lambda v: None)
        assert pv_client.unmonitor(key) is True
        mock_sub.close.assert_called_once()
        assert key not in pv_client._subscriptions

    def test_unmonitor_nonexistent_returns_false(self):
        assert pv_client.unmonitor("NOPE") is False

    @patch("iocmng.core.pv_client._get_context")
    def test_unmonitor_all(self, mock_ctx_fn):
        mock_ctx = MagicMock()
        mock_ctx.monitor.return_value = MagicMock()
        mock_ctx_fn.return_value = mock_ctx

        pv_client.monitor("A", lambda v: None)
        pv_client.monitor("B", lambda v: None)
        count = pv_client.unmonitor_all()
        assert count == 2
        assert len(pv_client._subscriptions) == 0

    @patch("iocmng.core.pv_client._get_context")
    def test_active_monitors(self, mock_ctx_fn):
        mock_ctx = MagicMock()
        mock_ctx.monitor.return_value = MagicMock()
        mock_ctx_fn.return_value = mock_ctx

        pv_client.monitor("X:PV", lambda v: None, name="x")
        monitors = pv_client.active_monitors()
        assert "x" in monitors


# ---------------------------------------------------------------------------
# REST API endpoint tests
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path):
    """Create a TestClient with pv_client mocked at the p4p level."""
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    # Reset pv_client state.
    pv_client._provider = "pva"
    pv_client._context = None
    pv_client._subscriptions.clear()

    app = create_app(plugins_dir=str(plugins_dir), disable_ophyd=True, pva=True)
    return TestClient(app)


class TestPvEndpoints:

    def test_get_provider(self, client):
        resp = client.get("/api/v1/pvs/provider")
        assert resp.status_code == 200
        assert resp.json()["provider"] == "pva"

    @patch("iocmng.core.pv_client._get_context")
    def test_pv_get_success(self, mock_ctx_fn, client):
        mock_ctx = MagicMock()
        mock_ctx.get.return_value = 3.14
        mock_ctx_fn.return_value = mock_ctx

        resp = client.post("/api/v1/pvs/get", json={"pv_name": "SIM:PV", "timeout": 2.0})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["value"] == 3.14

    @patch("iocmng.core.pv_client._get_context")
    def test_pv_get_timeout(self, mock_ctx_fn, client):
        mock_ctx = MagicMock()
        mock_ctx.get.side_effect = TimeoutError("timed out")
        mock_ctx_fn.return_value = mock_ctx

        resp = client.post("/api/v1/pvs/get", json={"pv_name": "BAD:PV"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert "timed out" in body["error"]

    @patch("iocmng.core.pv_client._get_context")
    def test_pv_get_value_with_todict(self, mock_ctx_fn, client):
        """p4p Value objects with .todict() should be converted."""
        mock_val = MagicMock()
        mock_val.todict.return_value = {"value": 123, "alarm": {}}
        mock_ctx = MagicMock()
        mock_ctx.get.return_value = mock_val
        mock_ctx_fn.return_value = mock_ctx

        resp = client.post("/api/v1/pvs/get", json={"pv_name": "PVA:PV"})
        body = resp.json()
        assert body["ok"] is True
        assert body["value"]["value"] == 123

    @patch("iocmng.core.pv_client._get_context")
    def test_pv_put_success(self, mock_ctx_fn, client):
        mock_ctx = MagicMock()
        mock_ctx_fn.return_value = mock_ctx

        resp = client.post("/api/v1/pvs/put", json={"pv_name": "SIM:PV", "value": 42})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        mock_ctx.put.assert_called_once_with("SIM:PV", 42, timeout=5.0)

    @patch("iocmng.core.pv_client._get_context")
    def test_pv_put_error(self, mock_ctx_fn, client):
        mock_ctx = MagicMock()
        mock_ctx.put.side_effect = RuntimeError("disconnected")
        mock_ctx_fn.return_value = mock_ctx

        resp = client.post("/api/v1/pvs/put", json={"pv_name": "SIM:PV", "value": 0})
        body = resp.json()
        assert body["ok"] is False
        assert "disconnected" in body["error"]

    @patch("iocmng.core.pv_client._get_context")
    def test_monitor_start_and_list(self, mock_ctx_fn, client):
        mock_ctx = MagicMock()
        mock_ctx.monitor.return_value = MagicMock()
        mock_ctx_fn.return_value = mock_ctx

        resp = client.post("/api/v1/pvs/monitor", json={"pv_name": "MON:PV", "name": "testmon"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["key"] == "testmon"

        resp = client.get("/api/v1/pvs/monitors")
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert "testmon" in body["monitors"]

    @patch("iocmng.core.pv_client._get_context")
    def test_monitor_stop(self, mock_ctx_fn, client):
        mock_ctx = MagicMock()
        mock_sub = MagicMock()
        mock_ctx.monitor.return_value = mock_sub
        mock_ctx_fn.return_value = mock_ctx

        client.post("/api/v1/pvs/monitor", json={"pv_name": "MON:PV", "name": "tostop"})
        resp = client.delete("/api/v1/pvs/monitor/tostop")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_sub.close.assert_called()

    def test_monitor_stop_nonexistent(self, client):
        resp = client.delete("/api/v1/pvs/monitor/nope")
        assert resp.status_code == 404

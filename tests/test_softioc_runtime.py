"""Tests for the soft IOC dispatcher selection."""

import builtins
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

from iocmng.core import softioc_runtime
from iocmng.core.softioc_runtime import ENV_VAR, dispatcher_name, ioc_init, make_dispatcher


@pytest.fixture
def fake_softioc(monkeypatch):
    """Replace the softioc package with mocks: no real IOC is started."""
    softioc_mod = MagicMock(name="softioc.softioc")
    dispatcher_mod = MagicMock(name="softioc.asyncio_dispatcher")
    package = types.ModuleType("softioc")
    package.softioc = softioc_mod
    package.asyncio_dispatcher = dispatcher_mod
    monkeypatch.setitem(sys.modules, "softioc", package)
    monkeypatch.setitem(sys.modules, "softioc.softioc", softioc_mod)
    monkeypatch.setitem(sys.modules, "softioc.asyncio_dispatcher", dispatcher_mod)
    monkeypatch.setattr(softioc_runtime, "_dispatcher", None)
    return softioc_mod, dispatcher_mod


def test_default_is_asyncio(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)
    assert dispatcher_name() == "asyncio"


def test_env_selects_dispatcher(monkeypatch):
    monkeypatch.setenv(ENV_VAR, " CoThread ")
    assert dispatcher_name() == "cothread"


def test_argument_wins_over_env(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "cothread")
    assert dispatcher_name("asyncio") == "asyncio"


def test_unknown_dispatcher(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "trio")
    with pytest.raises(ValueError, match="trio"):
        dispatcher_name()


def test_ioc_init_asyncio(fake_softioc, monkeypatch):
    softioc_mod, dispatcher_mod = fake_softioc
    monkeypatch.delenv(ENV_VAR, raising=False)

    assert ioc_init() == "asyncio"

    dispatcher = dispatcher_mod.AsyncioDispatcher.return_value
    softioc_mod.iocInit.assert_called_once_with(dispatcher)
    assert softioc_runtime._dispatcher is dispatcher  # kept alive


def test_ioc_init_cothread(fake_softioc, monkeypatch):
    softioc_mod, dispatcher_mod = fake_softioc
    monkeypatch.setitem(sys.modules, "cothread", types.ModuleType("cothread"))

    assert ioc_init("cothread") == "cothread"

    softioc_mod.iocInit.assert_called_once_with()
    dispatcher_mod.AsyncioDispatcher.assert_not_called()


def test_cothread_not_installed(fake_softioc, monkeypatch):
    monkeypatch.delitem(sys.modules, "cothread", raising=False)
    real_import = builtins.__import__

    def no_cothread(name, *args, **kwargs):
        if name == "cothread":
            raise ImportError("No module named 'cothread'")
        return real_import(name, *args, **kwargs)

    with patch.object(builtins, "__import__", no_cothread):
        with pytest.raises(RuntimeError, match=r"iocmng\[cothread\]"):
            make_dispatcher("cothread")

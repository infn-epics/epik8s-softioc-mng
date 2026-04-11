"""Core IOC manager components.

Exports are resolved lazily to avoid import cycles between the controller,
base runtime classes, and shared plugin-spec helpers.
"""

__all__ = ["IocMngController", "PluginLoader", "PluginValidator"]


def __getattr__(name):
	if name == "IocMngController":
		from iocmng.core.controller import IocMngController

		return IocMngController
	if name == "PluginLoader":
		from iocmng.core.loader import PluginLoader

		return PluginLoader
	if name == "PluginValidator":
		from iocmng.core.validator import PluginValidator

		return PluginValidator
	raise AttributeError(name)

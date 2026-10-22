"""Import every module that registers things at import time (sources, tasks, builders, commands).

Packages are optional until the task that creates them lands; import errors inside a module that
exists are real bugs and propagate.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil

PLUGIN_PACKAGES = (
    "marden.readmodels",
    "marden.seed",
    "marden.collectors",
    "marden.extraction",
    "marden.models",
    "marden.alerts",
    "marden.agents",
    "marden.facilities",
)

_loaded = False


def _package_exists(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def load_plugins(packages: tuple[str, ...] = PLUGIN_PACKAGES) -> list[str]:
    """Import all submodules of the plugin packages (once). Returns the module names imported."""
    global _loaded
    imported: list[str] = []
    if _loaded and packages == PLUGIN_PACKAGES:
        return imported
    for package_name in packages:
        if not _package_exists(package_name):
            continue
        package = importlib.import_module(package_name)
        for info in pkgutil.walk_packages(package.__path__, prefix=f"{package_name}."):
            leaf = info.name.rsplit(".", 1)[1]
            if leaf.startswith("_"):
                continue
            importlib.import_module(info.name)
            imported.append(info.name)
    if packages == PLUGIN_PACKAGES:
        _loaded = True
    return imported

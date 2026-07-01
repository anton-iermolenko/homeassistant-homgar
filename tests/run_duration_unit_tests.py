#!/usr/bin/env python3
"""Regression tests for valve duration unit options."""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
for candidate in (Path(__file__).resolve().parent, Path.cwd(), Path("/config")):
    current = candidate
    while True:
        if (current / "custom_components" / "homgar" / "number.py").exists():
            ROOT = current
            break
        if current.parent == current:
            break
        current = current.parent
    if (ROOT / "custom_components" / "homgar" / "number.py").exists():
        break
sys.path.insert(0, str(ROOT))


def _install_homeassistant_stubs() -> None:
    """Install minimal HA module stubs for importing number.py on a host."""
    modules = {
        "homeassistant": types.ModuleType("homeassistant"),
        "homeassistant.components": types.ModuleType("homeassistant.components"),
        "homeassistant.components.number": types.ModuleType("homeassistant.components.number"),
        "homeassistant.config_entries": types.ModuleType("homeassistant.config_entries"),
        "homeassistant.const": types.ModuleType("homeassistant.const"),
        "homeassistant.core": types.ModuleType("homeassistant.core"),
        "homeassistant.helpers": types.ModuleType("homeassistant.helpers"),
        "homeassistant.helpers.entity_platform": types.ModuleType("homeassistant.helpers.entity_platform"),
        "homeassistant.helpers.restore_state": types.ModuleType("homeassistant.helpers.restore_state"),
        "homeassistant.helpers.update_coordinator": types.ModuleType("homeassistant.helpers.update_coordinator"),
    }
    for name, module in modules.items():
        sys.modules.setdefault(name, module)

    class _NumberEntity:
        pass

    class _ConfigEntry:
        pass

    class _HomeAssistant:
        pass

    class _RestoreEntity:
        pass

    class _CoordinatorEntity:
        pass

    modules["homeassistant.components.number"].NumberEntity = _NumberEntity
    modules["homeassistant.components.number"].NumberMode = SimpleNamespace(BOX="box")
    modules["homeassistant.config_entries"].ConfigEntry = _ConfigEntry
    modules["homeassistant.const"].UnitOfTime = SimpleNamespace(SECONDS="s", MINUTES="min")
    modules["homeassistant.core"].HomeAssistant = _HomeAssistant
    modules["homeassistant.helpers.entity_platform"].AddEntitiesCallback = object
    modules["homeassistant.helpers.restore_state"].RestoreEntity = _RestoreEntity
    modules["homeassistant.helpers.update_coordinator"].CoordinatorEntity = _CoordinatorEntity


def _load_module(module_name: str, relative_path: str):
    path = ROOT / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load module from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


_install_homeassistant_stubs()
sys.modules.setdefault("custom_components", types.ModuleType("custom_components"))
homgar_pkg = sys.modules.setdefault("custom_components.homgar", types.ModuleType("custom_components.homgar"))
homgar_pkg.__path__ = [str(ROOT / "custom_components" / "homgar")]

const = _load_module("custom_components.homgar.const", "custom_components/homgar/const.py")
_load_module("custom_components.homgar.decoder", "custom_components/homgar/decoder.py")
coordinator_stub = types.ModuleType("custom_components.homgar.coordinator")
coordinator_stub.HomGarCoordinator = object
sys.modules["custom_components.homgar.coordinator"] = coordinator_stub
number = _load_module("custom_components.homgar.number", "custom_components/homgar/number.py")

CONF_VALVE_DURATION_UNIT = const.CONF_VALVE_DURATION_UNIT
DEFAULT_VALVE_DURATION_UNIT = const.DEFAULT_VALVE_DURATION_UNIT
VALVE_DURATION_UNIT_MINUTES = const.VALVE_DURATION_UNIT_MINUTES
VALVE_DURATION_UNIT_SECONDS = const.VALVE_DURATION_UNIT_SECONDS

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        print(f"  ✅ {name}")
        PASS += 1
    else:
        print(f"  ❌ {name}: {detail}")
        FAIL += 1


def fake_coordinator(options: dict | None = None):
    return SimpleNamespace(_entry=SimpleNamespace(options=options or {}))


def main() -> int:
    print("\n🧪 Valve duration unit regression tests")

    check(
        "default duration unit is minutes",
        number._duration_unit_from_options(fake_coordinator()) == DEFAULT_VALVE_DURATION_UNIT,
    )
    check(
        "seconds option is honored",
        number._duration_unit_from_options(
            fake_coordinator({CONF_VALVE_DURATION_UNIT: VALVE_DURATION_UNIT_SECONDS})
        ) == VALVE_DURATION_UNIT_SECONDS,
    )
    check(
        "invalid option falls back to minutes",
        number._duration_unit_from_options(
            fake_coordinator({CONF_VALVE_DURATION_UNIT: "hours"})
        ) == VALVE_DURATION_UNIT_MINUTES,
    )
    check(
        "10 native minutes converts to 600 seconds",
        number._duration_seconds_from_native(10, VALVE_DURATION_UNIT_MINUTES) == 600,
    )
    check(
        "30 native seconds stays 30 seconds",
        number._duration_seconds_from_native(30, VALVE_DURATION_UNIT_SECONDS) == 30,
    )
    check(
        "600 seconds renders as 10 minutes",
        number._duration_native_from_seconds(600, VALVE_DURATION_UNIT_MINUTES) == 10,
    )
    check(
        "30 seconds renders as 30 seconds",
        number._duration_native_from_seconds(30, VALVE_DURATION_UNIT_SECONDS) == 30,
    )
    check(
        "minutes mode clamps to one minute minimum",
        number._clamp_duration_seconds(30, VALVE_DURATION_UNIT_MINUTES) == 60,
    )
    check(
        "seconds mode allows one-second minimum",
        number._clamp_duration_seconds(0, VALVE_DURATION_UNIT_SECONDS) == 1,
    )
    check(
        "seconds mode clamps to one-hour maximum",
        number._clamp_duration_seconds(7200, VALVE_DURATION_UNIT_SECONDS) == 3600,
    )

    total = PASS + FAIL
    print(f"\n{'=' * 50}")
    print(f"Valve duration unit results: {PASS}/{total} passed, {FAIL} failed")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

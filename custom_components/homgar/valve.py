from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.valve import (
    ValveEntity,
    ValveEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_GROUP_MULTI_ZONE_DEVICES,
    CONF_VALVE_DURATION_UNIT,
    DEFAULT_VALVE_DURATION_UNIT,
    VALVE_DURATION_UNIT_SECONDS,
    controller_device_identifier,
    format_port_device_name,
    format_port_entity_name,
    zone_device_identifier,
)
from .coordinator import HomGarCoordinator
from .decoder import get_valve_ports, uses_ble_valve_control

_LOGGER = logging.getLogger(__name__)

# Default run duration used when HA opens a valve without a duration entity.
DEFAULT_DURATION_SECONDS = 600  # 10 minutes
COMPLETION_REFRESH_GRACE_SECONDS = 5


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    coordinator: HomGarCoordinator = data["coordinator"]

    sensors_cfg = coordinator.data.get("sensors", {})
    entities: list[HomGarValveEntity] = []

    for key, info in sensors_cfg.items():
        model = info.get("model")
        valve_ports = get_valve_ports(model) if model else []
        if not valve_ports:
            continue

        # Create one valve entity per port declared in product_models.json dp[]
        for port in valve_ports:
            entities.append(
                HomGarValveEntity(coordinator, key, info, port)
            )
            _LOGGER.debug(
                "Creating valve entity: key=%s port=%s model=%s",
                key, port, model,
            )

    if entities:
        async_add_entities(entities)


class HomGarValveEntity(CoordinatorEntity, ValveEntity):
    """Represents a single irrigation zone on a HomGar valve hub."""

    _attr_should_poll = False
    _attr_reports_position = False
    _attr_supported_features = ValveEntityFeature.OPEN | ValveEntityFeature.CLOSE

    def __init__(
        self,
        coordinator: HomGarCoordinator,
        sensor_key: str,
        sensor_info: dict,
        zone_num: int,
    ) -> None:
        super().__init__(coordinator)
        self._sensor_key = sensor_key
        self._sensor_info = sensor_info
        self._zone_num = zone_num
        self._completion_unsub: CALLBACK_TYPE | None = None
        self._completion_generation = 0

        hid = sensor_info["hid"]
        mid = sensor_info["mid"]
        addr = sensor_info["addr"]
        sub_name = sensor_info.get("sub_name") or f"Valve Hub {addr}"

        self._attr_unique_id = f"rainpoint_{mid}_{addr}_zone{zone_num}"
        grouped = (
            self.coordinator._entry.options.get(CONF_GROUP_MULTI_ZONE_DEVICES, False)
            and len(get_valve_ports(sensor_info.get("model"))) > 1
        )
        self._attr_name = format_port_entity_name(
            sub_name,
            sensor_info,
            zone_num,
            "Valve" if grouped else None,
            use_device_prefix=grouped,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Cancel pending local completion checks when HA unloads the entity."""
        self._cancel_completion_check()
        await super().async_will_remove_from_hass()

    # ------------------------------------------------------------------
    # Coordinator data helpers
    # ------------------------------------------------------------------

    @property
    def _port_data(self) -> dict | None:
        sensors = self.coordinator.data.get("sensors", {})
        info = sensors.get(self._sensor_key)
        if not info:
            return None
        decoded = info.get("data")
        if not decoded:
            return None
        port = decoded.get(f"port_{self._zone_num}")
        if port is not None:
            return port
        # Single-port devices (HTV113FRF etc.) store valve fields at the top level
        if self._zone_num == 1 and "is_watering" in decoded:
            return decoded
        return None

    # ------------------------------------------------------------------
    # Entity properties
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        sensors = self.coordinator.data.get("sensors", {})
        info = sensors.get(self._sensor_key)
        if not info:
            return False
        decoded = info.get("data")
        if not decoded:
            return False
        return decoded.get("hub_online", True)

    @property
    def is_closed(self) -> bool | None:
        port = self._port_data
        if port is None:
            return None
        is_watering = port.get("is_watering")
        if is_watering is None:
            return None
        return not is_watering

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        port = self._port_data
        if port:
            dur = port.get("current_session_duration")
            if dur is not None:
                attrs["duration_seconds"] = dur
            attrs["valve_state"] = port.get("valve_state")
        
        # Add firmware version from sensor info
        sensors = self.coordinator.data.get("sensors", {})
        info = sensors.get(self._sensor_key) or {}
        firmware_version = info.get("firmware_version")
        if firmware_version:
            attrs["firmware_version"] = firmware_version
        
        # Add device timestamp from decoded data
        data = self.coordinator.data.get("sensors", {}).get(self._sensor_key, {}).get("data", {})
        if "device_timestamp" in data:
            attrs["device_timestamp"] = data["device_timestamp"]
            attrs["timestamp_method"] = data.get("timestamp_method")
            attrs["timestamp_source"] = data.get("timestamp_source", "server")
        elif "server_timestamp" in data:
            attrs["device_timestamp"] = data["server_timestamp"]
            attrs["timestamp_source"] = data.get("timestamp_source", "server")
        
        return attrs

    @property
    def device_info(self) -> dict[str, Any]:
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        sub_name = self._sensor_info.get("sub_name") or f"Valve Hub {addr}"
        model = self._sensor_info.get("model") or "Unknown"
        parent_ident = controller_device_identifier(self._sensor_info)
        if (
            self.coordinator._entry.options.get(CONF_GROUP_MULTI_ZONE_DEVICES, False)
            and len(get_valve_ports(model)) > 1
        ):
            return {
                "identifiers": {(DOMAIN, zone_device_identifier(mid, addr, self._zone_num))},
                "name": format_port_device_name(sub_name, self._sensor_info, self._zone_num),
                "manufacturer": "RainPoint",
                "model": model,
                "via_device": (DOMAIN, parent_ident),
            }
        if self._sensor_info.get("type_flag") == 1:
            return {
                "identifiers": {(DOMAIN, f"rainpoint_hub_{mid}")},
                "name": sub_name,
                "manufacturer": "RainPoint",
                "model": model,
            }
        return {
            "identifiers": {(DOMAIN, f"{mid}_{addr}")},
            "name": sub_name,
            "manufacturer": "RainPoint",
            "model": model,
        }

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    def _cancel_completion_check(self) -> None:
        """Cancel any pending HA-started run completion check."""
        self._completion_generation += 1
        if self._completion_unsub is not None:
            self._completion_unsub()
            self._completion_unsub = None

    def _schedule_completion_check(self, duration: int) -> None:
        """Refresh after a HA-started run should have ended.

        RainPoint does not always publish the stop promptly.  This gives
        MQTT/cloud one more chance to report the real idle state without
        masking a valve that still reports open.
        """
        if self._completion_unsub is not None:
            self._completion_unsub()
            self._completion_unsub = None
        self._completion_generation += 1
        generation = self._completion_generation
        delay = max(1, int(duration)) + COMPLETION_REFRESH_GRACE_SECONDS

        def _run_check(_now) -> None:
            self._completion_unsub = None
            self.hass.async_create_task(
                self._async_handle_completion_check(generation)
            )

        self._completion_unsub = async_call_later(self.hass, delay, _run_check)

    async def _async_handle_completion_check(self, generation: int) -> None:
        """Refresh after a requested duration without inventing device state."""
        if generation != self._completion_generation:
            return
        try:
            await self.coordinator.async_request_refresh()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Completion refresh failed for key=%s zone=%s: %s",
                self._sensor_key,
                self._zone_num,
                exc,
            )

        if generation != self._completion_generation:
            return
        port = self._port_data
        if port and port.get("is_watering") is True:
            _LOGGER.debug(
                "Valve still reports open after requested duration: key=%s zone=%s",
                self._sensor_key,
                self._zone_num,
            )

    async def _get_configured_duration_seconds(self) -> int:
        """Look up the companion duration number entity and convert its value to seconds.

        Falls back to DEFAULT_DURATION_SECONDS
        if neither the entity nor persisted integration storage is available.

        Uses the entity registry to resolve unique_id -> entity_id so the lookup
        is not sensitive to HA auto-generated entity_id naming."""
        from homeassistant.helpers import entity_registry as er
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        unique_id = f"rainpoint_{mid}_{addr}_zone{self._zone_num}_duration"
        registry = er.async_get(self.hass)
        entity_id = registry.async_get_entity_id("number", "homgar", unique_id)
        if entity_id:
            state = self.hass.states.get(entity_id)
            if state is not None:
                try:
                    value = float(state.state)
                    duration_unit = self.coordinator._entry.options.get(
                        CONF_VALVE_DURATION_UNIT,
                        DEFAULT_VALVE_DURATION_UNIT,
                    )
                    if duration_unit == VALVE_DURATION_UNIT_SECONDS:
                        return max(1, int(value))
                    return max(1, int(value * 60))
                except (ValueError, TypeError):
                    pass
        try:
            from .number import async_get_saved_duration_seconds

            saved_seconds = await async_get_saved_duration_seconds(
                self.hass,
                self.coordinator._entry.entry_id,
                mid,
                addr,
                self._zone_num,
            )
            if saved_seconds is not None:
                return max(1, int(saved_seconds))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Could not read saved duration for unique_id=%s: %s",
                unique_id,
                exc,
            )
        _LOGGER.debug(
            "Duration entity for unique_id=%s not found, falling back to default %ss",
            unique_id, DEFAULT_DURATION_SECONDS,
        )
        return DEFAULT_DURATION_SECONDS

    def _apply_response_state(self, raw_state: str | None) -> None:
        """Decode the state string returned by controlWorkMode and inject it
        into the coordinator data immediately, bypassing the poll cycle."""
        if not raw_state:
            return
        from .decoder import decode_payload
        model = self._sensor_info.get("model")
        decoded = decode_payload(model, raw_state) if model else {}
        if not decoded or "error" in decoded:
            return
        current = dict(self.coordinator.data)
        sensors = dict(current.get("sensors", {}))
        if self._sensor_key not in sensors:
            return
        entry = dict(sensors[self._sensor_key])
        entry["data"] = decoded
        sensors[self._sensor_key] = entry
        current["sensors"] = sensors
        self.coordinator.async_set_updated_data(current)

    def _apply_optimistic_port_update(self, is_watering: bool, duration: int | None = None) -> None:
        """Apply a minimal optimistic state update to the decoded valve data."""
        current = dict(self.coordinator.data)
        sensors = dict(current.get("sensors", {}))
        entry = sensors.get(self._sensor_key)
        if not entry:
            return
        entry = dict(entry)
        decoded = dict(entry.get("data") or {})

        if f"port_{self._zone_num}" in decoded:
            port_key = f"port_{self._zone_num}"
            port_data = dict(decoded.get(port_key) or {})
            target = port_data
            decoded[port_key] = port_data
        elif self._zone_num == 1:
            target = decoded
        else:
            return

        target["is_watering"] = is_watering
        if duration is not None:
            target["current_session_duration"] = duration

        if not is_watering:
            target["valve_state"] = "idle"
            target["current_session_duration"] = 0
            target.pop("event_time", None)
            target.pop("event_time2", None)
            target.pop("irrigation_end_time", None)
            target.pop("cycle_type", None)

        entry["data"] = decoded
        sensors[self._sensor_key] = entry
        current["sensors"] = sensors
        self.coordinator.async_set_updated_data(current)

    # ------------------------------------------------------------------
    async def async_open_valve(self, **kwargs: Any) -> None:
        if "duration" in kwargs:
            duration = int(kwargs["duration"])
        else:
            duration = await self._get_configured_duration_seconds()
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        
        # Extract device_name and product_key from hub data instead of sensor_info
        hubs = self.coordinator.data.get("hubs", [])
        hub = next((h for h in hubs if h.get("mid") == mid), {})
        device_name = hub.get("deviceName", "")
        product_key = hub.get("productKey", "")

        _LOGGER.debug(
            "Opening valve mid=%s addr=%s zone=%s duration=%ss",
            mid, addr, self._zone_num, duration,
        )

        hid = self._sensor_info.get("hid")
        client = self.coordinator._client
        model = self._sensor_info.get("model")
        if model and uses_ble_valve_control(model):
            response_state = await client.control_work_mode_dp(
                mid=mid,
                addr=addr,
                device_name=device_name,
                product_key=product_key,
                port=self._zone_num,
                mode=1,
                duration=duration,
                hid=hid,
            )
        else:
            response_state = await client.control_work_mode(
                mid=mid,
                addr=addr,
                device_name=device_name,
                product_key=product_key,
                port=self._zone_num,
                mode=1,
                duration=duration,
                hid=hid,
            )
        # Bypass _apply_response_state to avoid crash - use refresh instead
        await self.coordinator.async_request_refresh()
        
        # OPTIMISTIC LOCAL UPDATE to prevent UI desync
        self._apply_optimistic_port_update(True, duration)
        self._schedule_completion_check(duration)
        return

    async def async_close_valve(self, **kwargs: Any) -> None:
        self._cancel_completion_check()
        mid = self._sensor_info["mid"]
        addr = self._sensor_info["addr"]
        
        # Extract device_name and product_key from hub data instead of sensor_info
        hubs = self.coordinator.data.get("hubs", [])
        hub = next((h for h in hubs if h.get("mid") == mid), {})
        device_name = hub.get("deviceName", "")
        product_key = hub.get("productKey", "")

        _LOGGER.debug(
            "Closing valve mid=%s addr=%s zone=%s",
            mid, addr, self._zone_num,
        )

        hid = self._sensor_info.get("hid")
        client = self.coordinator._client
        model = self._sensor_info.get("model")
        if model and uses_ble_valve_control(model):
            response_state = await client.control_work_mode_dp(
                mid=mid,
                addr=addr,
                device_name=device_name,
                product_key=product_key,
                port=self._zone_num,
                mode=0,
                duration=0,
                hid=hid,
            )
        else:
            response_state = await client.control_work_mode(
                mid=mid,
                addr=addr,
                device_name=device_name,
                product_key=product_key,
                port=self._zone_num,
                mode=0,
                duration=0,
                hid=hid,
            )
        # Bypass _apply_response_state to avoid crash - use refresh instead
        await self.coordinator.async_request_refresh()
        
        # OPTIMISTIC LOCAL UPDATE to prevent UI desync
        self._apply_optimistic_port_update(False, 0)
        return

"""Sensors for the FurryTail litter box."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfMass, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_CAT_WEIGHT,
    ATTR_DEVICE_FAULT,
    ATTR_DEVICE_STATUS,
    ATTR_DO_NOT_DISTURB_DURATION,
    ATTR_INDICATOR_LIGHT_DURATION,
    ATTR_POOP_COUNT,
    ATTR_STAY_INTERVAL,
    ATTR_TASK_TYPE,
    DEVICE_STATUS,
    DOMAIN,
    grams_to_kilograms,
)
from .coordinator import FurryTailCoordinator


def _identity(value: Any) -> Any:
    """Return an attribute without conversion."""
    return value


def _device_status(value: Any) -> str | None:
    """Map known device states and preserve unknown raw values."""
    if value is None:
        return None
    try:
        numeric_value = int(value)
    except (TypeError, ValueError):
        return str(value)
    return DEVICE_STATUS.get(numeric_value, str(numeric_value))


@dataclass(frozen=True, kw_only=True)
class FurryTailSensorEntityDescription(SensorEntityDescription):
    """Describe a coordinator-backed FurryTail sensor."""

    attr_id: int
    value_fn: Callable[[Any], Any] = _identity


SENSORS: tuple[FurryTailSensorEntityDescription, ...] = (
    FurryTailSensorEntityDescription(
        key="cat_weight",
        name="Cat weight",
        attr_id=ATTR_CAT_WEIGHT,
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=grams_to_kilograms,
    ),
    FurryTailSensorEntityDescription(
        key="poop_count",
        name="Poop count",
        attr_id=ATTR_POOP_COUNT,
        native_unit_of_measurement="uses",
        state_class=SensorStateClass.TOTAL_INCREASING,
    ),
    FurryTailSensorEntityDescription(
        key="stay_interval",
        name="Stay interval",
        attr_id=ATTR_STAY_INTERVAL,
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    FurryTailSensorEntityDescription(
        key="device_status",
        name="Device status",
        attr_id=ATTR_DEVICE_STATUS,
        value_fn=_device_status,
    ),
    FurryTailSensorEntityDescription(
        key="task_type",
        name="Task type",
        attr_id=ATTR_TASK_TYPE,
    ),
    FurryTailSensorEntityDescription(
        key="device_fault",
        name="Device fault",
        attr_id=ATTR_DEVICE_FAULT,
    ),
    FurryTailSensorEntityDescription(
        key="do_not_disturb_duration",
        name="Do not disturb duration",
        attr_id=ATTR_DO_NOT_DISTURB_DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    FurryTailSensorEntityDescription(
        key="indicator_light_duration",
        name="Indicator light duration",
        attr_id=ATTR_INDICATOR_LIGHT_DURATION,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FurryTail sensors from a config entry."""
    coordinator: FurryTailCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        FurryTailSensor(coordinator, description) for description in SENSORS
    )


class FurryTailSensor(CoordinatorEntity[FurryTailCoordinator], SensorEntity):
    """Represent one FurryTail numeric or text attribute."""

    entity_description: FurryTailSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FurryTailCoordinator,
        description: FurryTailSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.mac}_{description.attr_id}"
        self._attr_device_info = coordinator.device_info

    @property
    def native_value(self) -> Any:
        """Return the parsed or mapped attribute value."""
        value = self.coordinator.data.get(str(self.entity_description.attr_id))
        return self.entity_description.value_fn(value)

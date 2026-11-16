"""Binary sensors for the FurryTail litter box."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_CAT_STATUS,
    ATTR_DEVICE_FAULT,
    ATTR_ONLINE_STATUS,
    DOMAIN,
)
from .coordinator import FurryTailCoordinator


def _equals_one(value: Any) -> bool | None:
    """Interpret a present attribute as the integer state one."""
    if value is None:
        return None
    return value is True or str(value) == "1"


def _is_true(value: Any) -> bool | None:
    """Interpret bool-like cloud values."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.lower() in ("1", "true")
    return bool(value)


def _is_nonzero(value: Any) -> bool | None:
    """Interpret any nonzero fault code as a problem."""
    if value is None:
        return None
    try:
        return int(value) != 0
    except (TypeError, ValueError):
        return bool(value)


@dataclass(frozen=True, kw_only=True)
class FurryTailBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describe a FurryTail binary sensor."""

    attr_id: int | str
    is_on_fn: Callable[[Any], bool | None]


BINARY_SENSORS: tuple[FurryTailBinarySensorEntityDescription, ...] = (
    FurryTailBinarySensorEntityDescription(
        key="cat_present",
        name="Cat present",
        attr_id=ATTR_CAT_STATUS,
        device_class=BinarySensorDeviceClass.OCCUPANCY,
        is_on_fn=_equals_one,
    ),
    FurryTailBinarySensorEntityDescription(
        key="online",
        name="Online",
        attr_id=ATTR_ONLINE_STATUS,
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=_is_true,
    ),
    FurryTailBinarySensorEntityDescription(
        key="fault",
        name="Fault",
        attr_id=ATTR_DEVICE_FAULT,
        device_class=BinarySensorDeviceClass.PROBLEM,
        is_on_fn=_is_nonzero,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FurryTail binary sensors from a config entry."""
    coordinator: FurryTailCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        FurryTailBinarySensor(coordinator, description)
        for description in BINARY_SENSORS
    )


class FurryTailBinarySensor(
    CoordinatorEntity[FurryTailCoordinator], BinarySensorEntity
):
    """Represent one FurryTail binary attribute."""

    entity_description: FurryTailBinarySensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FurryTailCoordinator,
        description: FurryTailBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.mac}_{description.attr_id}"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool | None:
        """Return the decoded binary state."""
        value = self.coordinator.data.get(str(self.entity_description.attr_id))
        return self.entity_description.is_on_fn(value)

"""Switch controls for the FurryTail litter box."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_AUTO_CLEAN_TIMER,
    ATTR_IS_AUTO_CLEAN,
    ATTR_IS_CHILD_LOCK_ON,
    ATTR_IS_CLEAN_SHAKE_ON,
    ATTR_IS_DO_NOT_DISTURB,
    ATTR_IS_INDICATOR_LIGHT_ON,
    DOMAIN,
)
from .coordinator import FurryTailCoordinator


def _as_bool(value: Any) -> bool:
    """Return a safe switch state for bool, string, integer, or None."""
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true")
    return bool(value)


@dataclass(frozen=True, kw_only=True)
class FurryTailSwitchEntityDescription(SwitchEntityDescription):
    """Describe a writable FurryTail boolean attribute."""

    attr_id: int


SWITCHES: tuple[FurryTailSwitchEntityDescription, ...] = (
    FurryTailSwitchEntityDescription(
        key="auto_clean", name="Auto clean", attr_id=ATTR_IS_AUTO_CLEAN
    ),
    FurryTailSwitchEntityDescription(
        key="auto_clean_timer",
        name="Auto clean timer",
        attr_id=ATTR_AUTO_CLEAN_TIMER,
    ),
    FurryTailSwitchEntityDescription(
        key="do_not_disturb",
        name="Do not disturb",
        attr_id=ATTR_IS_DO_NOT_DISTURB,
    ),
    FurryTailSwitchEntityDescription(
        key="indicator_light",
        name="Indicator light",
        attr_id=ATTR_IS_INDICATOR_LIGHT_ON,
    ),
    FurryTailSwitchEntityDescription(
        key="clean_shake", name="Clean shake", attr_id=ATTR_IS_CLEAN_SHAKE_ON
    ),
    FurryTailSwitchEntityDescription(
        key="child_lock", name="Child lock", attr_id=ATTR_IS_CHILD_LOCK_ON
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FurryTail switches from a config entry."""
    coordinator: FurryTailCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        FurryTailSwitch(coordinator, description) for description in SWITCHES
    )


class FurryTailSwitch(CoordinatorEntity[FurryTailCoordinator], SwitchEntity):
    """Represent one writable FurryTail boolean attribute."""

    entity_description: FurryTailSwitchEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FurryTailCoordinator,
        description: FurryTailSwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.mac}_{description.attr_id}"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool:
        """Return the current bool-like state."""
        value = self.coordinator.data.get(str(self.entity_description.attr_id))
        return _as_bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the feature."""
        await self.coordinator.async_set_attribute(
            self.entity_description.attr_id, 1
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the feature."""
        await self.coordinator.async_set_attribute(
            self.entity_description.attr_id, 0
        )

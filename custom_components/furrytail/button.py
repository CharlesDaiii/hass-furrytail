"""One-shot controls for the FurryTail litter box."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_FLATTEN_STATUS,
    ATTR_MANUAL_CLEAN_STATUS,
    ATTR_POOP_STORAGE_RESET_STATUS,
    ATTR_ROLLER_RESET_STATUS,
    ATTR_SAND_CHANGING_STATUS,
    DOMAIN,
)
from .coordinator import FurryTailCoordinator


@dataclass(frozen=True, kw_only=True)
class FurryTailButtonEntityDescription(ButtonEntityDescription):
    """Describe a trigger-style FurryTail attribute."""

    attr_id: int


BUTTONS: tuple[FurryTailButtonEntityDescription, ...] = (
    FurryTailButtonEntityDescription(
        key="manual_clean", name="Manual clean", attr_id=ATTR_MANUAL_CLEAN_STATUS
    ),
    FurryTailButtonEntityDescription(
        key="flatten", name="Flatten", attr_id=ATTR_FLATTEN_STATUS
    ),
    FurryTailButtonEntityDescription(
        key="sand_change", name="Sand change", attr_id=ATTR_SAND_CHANGING_STATUS
    ),
    FurryTailButtonEntityDescription(
        key="reset_poop_storage",
        name="Reset poop storage",
        attr_id=ATTR_POOP_STORAGE_RESET_STATUS,
    ),
    FurryTailButtonEntityDescription(
        key="roller_reset", name="Roller reset", attr_id=ATTR_ROLLER_RESET_STATUS
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up FurryTail buttons from a config entry."""
    coordinator: FurryTailCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        FurryTailButton(coordinator, description) for description in BUTTONS
    )


class FurryTailButton(CoordinatorEntity[FurryTailCoordinator], ButtonEntity):
    """Trigger a FurryTail action by writing the value one.

    Value 1 is confirmed for manual clean (attribute 3). Its use for flatten,
    sand change, poop-storage reset, and roller reset is inferred by analogy
    and remains an unverified per-attribute limitation.
    """

    entity_description: FurryTailButtonEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FurryTailCoordinator,
        description: FurryTailButtonEntityDescription,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{coordinator.mac}_{description.attr_id}"
        self._attr_device_info = coordinator.device_info

    async def async_press(self) -> None:
        """Write the trigger value one."""
        await self.coordinator.async_set_attribute(
            self.entity_description.attr_id, 1
        )

"""binary_sensor 平台：服务器在线。"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, KEY_ONLINE
from .coordinator import McServerCoordinator
from .entity import McServerEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
) -> None:
    coordinator: McServerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([OnlineBinarySensor(coordinator, entry, KEY_ONLINE)])


class OnlineBinarySensor(McServerEntity, BinarySensorEntity):
    """服务器在线（exporter 快照 server.online：进程存活且端口/服务就绪）。

    值=off 表示「服务器离线（正常态）」；导出器本身不可达时实体 unavailable
    （CoordinatorEntity 的 available 语义），不要用 unavailable 判服务器离线。
    """

    _attr_translation_key = "online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool:
        return self._server_online

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "pid": self._server.get("pid"),
            "uptime_s": self._server.get("uptime_s"),
            "port_open": self._server.get("port_open"),
            "rcon_error": self._rcon.get("error"),
        }

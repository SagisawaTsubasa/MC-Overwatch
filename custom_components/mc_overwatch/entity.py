"""实体基类：设备信息 + 快照便捷访问器。"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import McServerCoordinator


def as_dict(v) -> dict:
    """契约里该为 dict 的字段若类型不符（exporter 侧异常），兜底为空 dict。"""
    return v if isinstance(v, dict) else {}


def as_num(v):
    """数值字段类型不符时返回 None（配合 available 判定，防 round()/算术炸实体）。"""
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


class McServerEntity(CoordinatorEntity[McServerCoordinator]):
    """挂到单一设备「MC 服务器」下的实体基类。

    available 基础版 = coordinator 最近一次刷新成功（CoordinatorEntity 提供），
    各实体按需叠加自己的可用性规则。
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: McServerCoordinator, entry: ConfigEntry, key: str
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}-{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="MC 服务器",
            manufacturer="luzeyi",
            model="mc-ha-exporter",
        )

    @property
    def _data(self) -> dict:
        return as_dict(self.coordinator.data)

    @property
    def _server(self) -> dict:
        return as_dict(self._data.get("server"))

    @property
    def _rcon(self) -> dict:
        return as_dict(self._data.get("rcon"))

    @property
    def _monitor(self) -> dict | None:
        mon = self._data.get("monitor")
        return mon if isinstance(mon, dict) else None

    @property
    def _server_online(self) -> bool:
        return bool(self._server.get("online"))

    @property
    def _rcon_ok(self) -> bool:
        return bool(self._rcon.get("ok"))

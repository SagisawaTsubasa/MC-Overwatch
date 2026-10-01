"""sensor 平台：玩家 / TPS / MSPT / 内存 / CPU / 运行时长 / 巡检判级。"""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    KEY_CPU,
    KEY_MEMORY,
    KEY_MSPT,
    KEY_PLAYERS,
    KEY_TPS,
    KEY_UPTIME,
    KEY_VERDICT,
)
from .coordinator import McServerCoordinator
from .entity import McServerEntity, as_dict, as_num


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: McServerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            PlayersSensor(coordinator, entry, KEY_PLAYERS),
            TpsSensor(coordinator, entry, KEY_TPS),
            MsptSensor(coordinator, entry, KEY_MSPT),
            MemorySensor(coordinator, entry, KEY_MEMORY),
            CpuSensor(coordinator, entry, KEY_CPU),
            UptimeSensor(coordinator, entry, KEY_UPTIME),
            VerdictSensor(coordinator, entry, KEY_VERDICT),
        ]
    )


class PlayersSensor(McServerEntity, SensorEntity):
    """在线玩家数；属性带名单。"""

    _attr_translation_key = "players"
    _attr_native_unit_of_measurement = "人"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:account-multiple"

    @property
    def available(self) -> bool:
        return super().available and self._server_online and self._rcon_ok

    @property
    def native_value(self):
        players = as_dict(self._rcon.get("players"))
        return as_num(players.get("online"))

    @property
    def extra_state_attributes(self) -> dict:
        players = as_dict(self._rcon.get("players"))
        return {
            "names": players.get("names") or [],
            "max": as_num(players.get("max")),
        }


class TpsSensor(McServerEntity, SensorEntity):
    """TPS：由 tick query 的 MSPT 换算（封顶 20；spark 经 RCON 无回显）。"""

    _attr_translation_key = "tps"
    _attr_native_unit_of_measurement = "TPS"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:speedometer"

    def _tps(self) -> dict:
        return as_dict(self._rcon.get("tps"))

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._server_online
            and self._rcon_ok
            and as_num(self._tps().get("tps_5m")) is not None
        )

    @property
    def native_value(self):
        return as_num(self._tps().get("tps_5m"))

    @property
    def extra_state_attributes(self) -> dict:
        tps = self._tps()
        return {
            "tps_1m": as_num(tps.get("tps_1m")),
            "tps_15m": as_num(tps.get("tps_15m")),
            "mspt": as_num(tps.get("mspt")),
            "raw": tps.get("raw"),
        }


class MsptSensor(McServerEntity, SensorEntity):
    """每 tick 耗时（毫秒）。"""

    _attr_translation_key = "mspt"
    _attr_native_unit_of_measurement = "ms"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:timer-outline"

    def _value(self):
        return as_num(as_dict(self._rcon.get("tps")).get("mspt"))

    @property
    def available(self) -> bool:
        return super().available and self._server_online and self._value() is not None

    @property
    def native_value(self):
        return self._value()

    @property
    def extra_state_attributes(self) -> dict:
        return {"raw": as_dict(self._rcon.get("tps")).get("raw")}


class MemorySensor(McServerEntity, SensorEntity):
    """JVM 进程 RSS（GB）。"""

    _attr_translation_key = "memory"
    _attr_device_class = SensorDeviceClass.DATA_SIZE
    _attr_native_unit_of_measurement = "GB"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def _value(self):
        return as_num(self._server.get("rss_bytes"))

    @property
    def available(self) -> bool:
        return super().available and self._server_online and self._value() is not None

    @property
    def native_value(self):
        v = self._value()
        return round(v / 1024**3, 2) if v is not None else None


class CpuSensor(McServerEntity, SensorEntity):
    """进程 CPU 占用（ps pcpu 口径）。"""

    _attr_translation_key = "cpu"
    _attr_native_unit_of_measurement = "%"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cpu-64-bit"

    def _value(self):
        return as_num(self._server.get("cpu_percent"))

    @property
    def available(self) -> bool:
        return super().available and self._server_online and self._value() is not None

    @property
    def native_value(self):
        v = self._value()
        return round(v, 1) if v is not None else None


class UptimeSensor(McServerEntity, SensorEntity):
    """运行时长：时间戳设备类（HA 显示为『x 小时前』并自行走秒）。"""

    _attr_translation_key = "uptime"
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._server_online
            and isinstance(self._server.get("started_at"), str)
        )

    @property
    def native_value(self):
        v = self._server.get("started_at")
        return dt_util.parse_datetime(v) if isinstance(v, str) else None

    @property
    def extra_state_attributes(self) -> dict:
        return {"uptime_s": as_num(self._server.get("uptime_s"))}


class VerdictSensor(McServerEntity, SensorEntity):
    """每小时巡检判级（check.py 透传）：OK / WARN / FAULT-*。"""

    _attr_translation_key = "verdict"
    _attr_icon = "mdi:heartbeat-pulse"

    @property
    def available(self) -> bool:
        return super().available and self._monitor is not None

    @property
    def native_value(self):
        return self._monitor.get("verdict")

    @property
    def extra_state_attributes(self) -> dict:
        mon = self._monitor or {}
        return {
            "events_1h": as_dict(mon.get("events_1h")),
            "chunky": mon.get("chunky"),
            "checked_at": mon.get("checked_at"),
        }

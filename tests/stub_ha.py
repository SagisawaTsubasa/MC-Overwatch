"""离线 stub：把 homeassistant / aiohttp / voluptuous 顶替成最小实现，
让本测试机（无 HA 环境）也能 import 集成模块并构造实体。

只覆盖集成代码实际用到的表面；不追求行为等价，只追求 import + 构造不炸。
"""

import sys
import types
from datetime import datetime

_UNDEFINED = object()


# ---------------------------------------------------------------- voluptuous
vol = types.ModuleType("voluptuous")


class VolInvalid(Exception):
    pass


vol.Invalid = VolInvalid


def _coerce(t):
    return lambda v: t(v)


vol.Coerce = _coerce


class Range:
    def __init__(self, min=None, max=None):
        self.min, self.max = min, max

    def __call__(self, v):
        if (self.min is not None and v < self.min) or (
            self.max is not None and v > self.max
        ):
            raise VolInvalid(f"value out of range [{self.min}, {self.max}]: {v}")
        return v


vol.Range = Range


class All:
    def __init__(self, *fns):
        self.fns = fns

    def __call__(self, v):
        for fn in self.fns:
            v = fn(v)
        return v


vol.All = All


class Required:
    def __init__(self, key, default=_UNDEFINED):
        self.key, self.default = key, default


vol.Required = Required


class Schema:
    def __init__(self, schema):
        self.schema = schema

    def __call__(self, data):
        if not isinstance(data, dict):
            raise VolInvalid("not a dict")
        out = dict(data)
        for key, fn in self.schema.items():
            if isinstance(key, Required):
                if key.key in out:
                    out[key.key] = fn(out[key.key])
                elif key.default is not _UNDEFINED:
                    out[key.key] = fn(key.default)
                else:
                    raise VolInvalid(f"required key missing: {key.key}")
        return out


vol.Schema = Schema

# ---------------------------------------------------------------- aiohttp
aiohttp = types.ModuleType("aiohttp")


class ClientError(Exception):
    pass


class ClientSession:
    pass


aiohttp.ClientError = ClientError
aiohttp.ClientSession = ClientSession
aiohttp.ContentTypeError = type("ContentTypeError", (ClientError,), {})

# ---------------------------------------------------------------- homeassistant
ha = types.ModuleType("homeassistant")
sys.modules.setdefault("homeassistant", ha)


def _mod(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


class ConfigEntryState:
    LOADED = "loaded"
    NOT_LOADED = "not_loaded"
    SETUP_ERROR = "setup_error"
    SETUP_RETRY = "setup_retry"


class ConfigEntry:
    def __init__(
        self,
        entry_id="test-entry",
        data=None,
        options=None,
        state=ConfigEntryState.LOADED,
        unique_id=None,
    ):
        self.entry_id = entry_id
        self.data = data or {}
        self.options = options or {}
        self.state = state
        self.unique_id = unique_id


class ConfigFlowBase:
    def __init_subclass__(cls, **kwargs):  # 容忍 class ... domain=DOMAIN 传参
        kwargs.pop("domain", None)
        super().__init_subclass__(**kwargs)

    def __init__(self):
        # 模拟 HA flow 框架注入的最小上下文
        self.context = {}
        self.handler = None
        self.hass = None
        self.unique_id = None

    @property
    def _reauth_entry_id(self):
        """HA ≥2024.12 行为：只读 property，子类赋值会 AttributeError。"""
        return self.context.get("entry_id")

    async def async_set_unique_id(self, uid):
        self.unique_id = uid

    def _abort_if_unique_id_configured(self, *a, **kw):
        pass

    def async_show_form(self, step_id, data_schema=None, errors=None):
        return {
            "type": "form",
            "step_id": step_id,
            "data_schema": data_schema,
            "errors": errors,
        }

    def async_create_entry(self, title, data):
        return {"type": "create_entry", "title": title, "data": data}

    def async_abort(self, reason):
        return {"type": "abort", "reason": reason}


ce = _mod("homeassistant.config_entries")
ce.ConfigEntry = ConfigEntry
ce.ConfigEntryState = ConfigEntryState
ce.ConfigFlow = ConfigFlowBase
ce.OptionsFlow = type("OptionsFlow", (ConfigFlowBase,), {})

core = _mod("homeassistant.core")
core.HomeAssistant = type("HomeAssistant", (), {})

const = _mod("homeassistant.const")
const.Platform = type(
    "Platform", (), {"BINARY_SENSOR": "binary_sensor", "SENSOR": "sensor"}
)

exc = _mod("homeassistant.exceptions")


class HomeAssistantError(Exception):
    pass


class ConfigEntryAuthFailed(HomeAssistantError):
    pass


exc.HomeAssistantError = HomeAssistantError
exc.ConfigEntryAuthFailed = ConfigEntryAuthFailed

uc = _mod("homeassistant.helpers.update_coordinator")


class DataUpdateCoordinator:
    __class_getitem__ = classmethod(lambda cls, item: cls)

    def __init__(self, hass, logger, name=None, update_interval=None):
        self.hass = hass
        self.update_interval = update_interval


class CoordinatorEntity:
    __class_getitem__ = classmethod(lambda cls, item: cls)

    def __init__(self, coordinator):
        self.coordinator = coordinator

    @property
    def available(self):
        return getattr(self.coordinator, "last_update_success", True)


class UpdateFailed(HomeAssistantError):
    pass


uc.DataUpdateCoordinator = DataUpdateCoordinator
uc.CoordinatorEntity = CoordinatorEntity
uc.UpdateFailed = UpdateFailed

helpers = _mod("homeassistant.helpers")
_mod("homeassistant.helpers.entity_platform").AddEntitiesCallback = object
_mod("homeassistant.helpers.aiohttp_client").async_get_clientsession = lambda hass: None
dr = _mod("homeassistant.helpers.device_registry")


class DeviceInfo(dict):
    def __init__(self, **kw):
        super().__init__(**kw)


dr.DeviceInfo = DeviceInfo

util = _mod("homeassistant.util")


class _DT:
    @staticmethod
    def parse_datetime(s):
        try:
            return datetime.fromisoformat(s)
        except (TypeError, ValueError):
            return None


util.dt = _DT()

sensor_mod = _mod("homeassistant.components.sensor")


class _Enum:
    DATA_SIZE = "data_size"
    TIMESTAMP = "timestamp"


sensor_mod.SensorDeviceClass = _Enum
sensor_mod.SensorStateClass = type(
    "SensorStateClass", (), {"MEASUREMENT": "measurement"}
)
sensor_mod.SensorEntity = type("SensorEntity", (), {})

bs_mod = _mod("homeassistant.components.binary_sensor")
bs_mod.BinarySensorDeviceClass = type("BSDC", (), {"CONNECTIVITY": "connectivity"})
bs_mod.BinarySensorEntity = type("BinarySensorEntity", (), {})

_mod("homeassistant.data_entry_flow").FlowResult = dict
ha.config_entries = ce
ha.const = const
ha.exceptions = exc
ha.util = util
ha.components = types.SimpleNamespace(sensor=sensor_mod, binary_sensor=bs_mod)

sys.modules.setdefault("aiohttp", aiohttp)
sys.modules.setdefault("voluptuous", vol)

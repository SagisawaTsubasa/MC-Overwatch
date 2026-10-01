"""config flow：UI 配置 + reconfigure + options + reauth。

- user：首次添加（host/port/token/刷新间隔，实测一次 /status.json）。
- reconfigure：改 host/port/token（Mac 换 IP / token 轮换），实体与历史不换。
- options：改刷新间隔。
- reauth：token 失效（coordinator 抛 ConfigEntryAuthFailed 后由 HA 发起）。

版本兼容注记：
- reauth 一律从 self.context["entry_id"] 取 entry，绝不给 _reauth_entry_id 赋值
  （HA ≥2024.12 它是只读 property，赋值直接 AttributeError 且会卡死 reauth 流程）。
- options flow 读 entry：新版用注入的 self.config_entry；2024.8–2024.11 的
  options context 不带 entry_id，得用 self.handler（handler 就是 entry_id）。
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .coordinator import CannotConnect, InvalidAuth, async_fetch_status

LOGGER = logging.getLogger(__name__)

PORT_SCHEMA = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
SCAN_INTERVAL_SCHEMA = vol.All(
    vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): PORT_SCHEMA,
        vol.Required(CONF_TOKEN): str,
        vol.Required(
            CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL
        ): SCAN_INTERVAL_SCHEMA,
    }
)


def parse_host_port(host: str, port: int) -> tuple[str, int]:
    """容忍粘贴 http://ip:port/ 或 ip:port 形式；输入里带显式端口时优先于 port 字段。

    非法输入（坏端口/怪地址）会抛 ValueError——调用方必须接住给表单错误，
    不能让它冒泡进 flow 框架（HA 只捕获 AbortFlow）。"""
    raw = host.strip().rstrip("/")
    if "://" not in raw:
        raw = "http://" + raw  # 无 scheme 时补上，让 urlsplit 认出 :port
    parts = urlsplit(raw)
    hostname = parts.hostname or raw  # 解析失败退回原文，交给实测一步报错
    return hostname, parts.port or port


async def validate_input(hass: HomeAssistant, host: str, port: int, token: str) -> None:
    """实测一次 /status.json；失败抛 CannotConnect / InvalidAuth。"""
    session = async_get_clientsession(hass)
    url = f"http://{host}:{port}/status.json"
    await async_fetch_status(session, url, token)


def _current_interval(entry: ConfigEntry | None) -> int:
    """表单"当前值"：options 优先（用户改过的），entry.data 兜底（首次添加值）。"""
    if entry is None:
        return DEFAULT_SCAN_INTERVAL
    return int(
        entry.options.get(
            CONF_SCAN_INTERVAL,
            entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )
    )


class McServerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """处理 UI 添加集成。"""

    VERSION = 1

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> McServerOptionsFlow:
        return McServerOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = port = None
            try:
                host, port = parse_host_port(
                    user_input[CONF_HOST], int(user_input[CONF_PORT])
                )
            except (ValueError, TypeError):
                errors["base"] = "cannot_connect"  # 地址格式非法，提示检查主机/端口
            if host is not None:
                await self.async_set_unique_id(f"{host}:{port}")
                self._abort_if_unique_id_configured()
                try:
                    await validate_input(self.hass, host, port, user_input[CONF_TOKEN])
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001
                    LOGGER.exception("配置校验出现意外错误")
                    errors["base"] = "unknown"
                else:
                    return self.async_create_entry(
                        title=f"MC 服务器 ({host})",
                        data={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_TOKEN: user_input[CONF_TOKEN],
                            CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL]),
                        },
                    )
        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    # ------------------------------------------------------------ reauth
    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """token 失效（403）：只问一个新 token。"""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        errors: dict[str, str] = {}
        if user_input is not None and entry is not None:
            token = user_input[CONF_TOKEN]
            try:
                await validate_input(
                    self.hass,
                    entry.data[CONF_HOST],
                    entry.data[CONF_PORT],
                    token,
                )
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                self.hass.config_entries.async_update_entry(
                    entry, data={**entry.data, CONF_TOKEN: token}
                )
                await self._async_reload_if_not_loaded(entry)
                return self.async_abort(reason="reauth_successful")
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_TOKEN): str}),
            errors=errors,
        )

    # ------------------------------------------------------------ reconfigure
    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self.hass.config_entries.async_get_entry(self.context.get("entry_id"))
        errors: dict[str, str] = {}
        if user_input is not None and entry is not None:
            host = port = None
            try:
                host, port = parse_host_port(
                    user_input[CONF_HOST], int(user_input[CONF_PORT])
                )
            except (ValueError, TypeError):
                errors["base"] = "cannot_connect"
            if host is not None:
                await self.async_set_unique_id(f"{host}:{port}")
                if self.unique_id != entry.unique_id:
                    self._abort_if_unique_id_configured()
                try:
                    await validate_input(self.hass, host, port, user_input[CONF_TOKEN])
                except InvalidAuth:
                    errors["base"] = "invalid_auth"
                except CannotConnect:
                    errors["base"] = "cannot_connect"
                except Exception:  # noqa: BLE001
                    LOGGER.exception("重新配置校验出现意外错误")
                    errors["base"] = "unknown"
                else:
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data={
                            CONF_HOST: host,
                            CONF_PORT: port,
                            CONF_TOKEN: user_input[CONF_TOKEN],
                            CONF_SCAN_INTERVAL: _current_interval(entry),
                        },
                        unique_id=f"{host}:{port}",  # entry 的 unique_id 同步迁移
                    )
                    await self._async_reload_if_not_loaded(entry)
                    return self.async_abort(reason="reconfigure_successful")
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._reconfigure_schema(entry),
            errors=errors,
        )

    def _reconfigure_schema(self, entry: ConfigEntry | None) -> vol.Schema:
        d = (entry.data if entry else {}) or {}
        return vol.Schema(
            {
                vol.Required(CONF_HOST, default=d.get(CONF_HOST, "")): str,
                vol.Required(
                    CONF_PORT, default=d.get(CONF_PORT, DEFAULT_PORT)
                ): PORT_SCHEMA,
                vol.Required(CONF_TOKEN, default=d.get(CONF_TOKEN, "")): str,
            }
        )

    async def _async_reload_if_not_loaded(self, entry: ConfigEntry) -> None:
        """reauth/reconfigure 只靠 update listener reload 的前提是 listener 已注册
        （首刷成功后才有）——entry 处于 SETUP_ERROR 等未加载态时必须显式 reload，
        否则修好后集成一直不可用。"""
        if entry.state is not ConfigEntryState.LOADED:
            await self.hass.config_entries.async_reload(entry.entry_id)


class McServerOptionsFlow(config_entries.OptionsFlow):
    """调整刷新间隔（写 entry.options，不重建 entry，历史不丢）。"""

    def _entry(self) -> ConfigEntry | None:
        # HA ≥2024.12 注入 self.config_entry；旧版从 context/handler 反查
        entry = getattr(self, "config_entry", None)
        if entry is not None:
            return entry
        return self.hass.config_entries.async_get_entry(
            self.context.get("entry_id") or self.handler
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        entry = self._entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                interval = SCAN_INTERVAL_SCHEMA(user_input[CONF_SCAN_INTERVAL])
            except vol.Invalid:
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(
                    title="", data={CONF_SCAN_INTERVAL: interval}
                )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL, default=_current_interval(entry)
                    ): SCAN_INTERVAL_SCHEMA
                }
            ),
            errors=errors,
        )

"""DataUpdateCoordinator：轮询 Mac 上 mc-ha-exporter 的 /status.json。

exporter 不可达/超时/非 JSON → UpdateFailed（全部实体 unavailable，
区别于"MC 服务器离线"——后者快照正常、实体仍可用值为 off）；
token 被拒（403）→ ConfigEntryAuthFailed（HA 自动进入重认证流程）。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_HOST,
    CONF_PORT,
    CONF_SCAN_INTERVAL,
    CONF_TOKEN,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    REQUEST_TIMEOUT,
)

LOGGER = logging.getLogger(__package__)


class McServerError(Exception):
    """基础异常。"""


class CannotConnect(McServerError):
    """连不上 exporter 或响应异常。"""


class InvalidAuth(McServerError):
    """token 被拒（403）。"""


async def async_fetch_status(
    session: aiohttp.ClientSession, url: str, token: str
) -> dict:
    """GET /status.json 并解析。失败统一抛 CannotConnect / InvalidAuth，供
    coordinator 与 config flow 共用。token 只走 header，不进 URL（避免随
    错误信息/日志泄露）。"""
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT):
            async with session.get(url, headers={"X-Export-Token": token}) as resp:
                if resp.status == 403:
                    raise InvalidAuth
                resp.raise_for_status()
                data = await resp.json(content_type=None)
    except InvalidAuth:
        raise
    except TimeoutError as err:
        raise CannotConnect("timeout") from err
    except aiohttp.ClientError as err:
        raise CannotConnect(str(err) or err.__class__.__name__) from err
    except ValueError as err:  # json.JSONDecodeError（如代理返回 HTML）
        raise CannotConnect("响应不是 JSON") from err
    if not isinstance(data, dict) or "server" not in data:
        raise CannotConnect("unexpected payload")
    return data


class McServerCoordinator(DataUpdateCoordinator[dict]):
    """每 scan_interval 秒拉一次 status.json 快照（dict，契约见仓库 README）。"""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self._session = async_get_clientsession(hass)
        self._url = (
            f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}/status.json"
        )
        self._token = entry.data[CONF_TOKEN]
        super().__init__(
            hass,
            LOGGER,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=int(
                    entry.options.get(
                        CONF_SCAN_INTERVAL,
                        entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
                    )
                )
            ),
        )

    async def _async_update_data(self) -> dict:
        try:
            return await async_fetch_status(self._session, self._url, self._token)
        except InvalidAuth as err:
            raise ConfigEntryAuthFailed("token 被拒（403），请更新令牌") from err
        except CannotConnect as err:
            raise UpdateFailed(f"exporter 不可达: {err}") from err

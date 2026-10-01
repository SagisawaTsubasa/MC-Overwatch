#!/usr/bin/env python3
"""config flow 三条流程的离线测试（reauth / reconfigure / options）。

背景：2026-09-28 复审发现 reauth 给 _reauth_entry_id 赋值在 HA ≥2024.12 必崩
（只读 property）、options flow 在 2024.8–2024.11 的 context 没有 entry_id。
stub_ha 现已把这两个行为建模进去（赋值会 AttributeError、context 不带 entry_id），
这类回归以后会被这里逮住。

运行：python3 tests/test_flows.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stub_ha  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stub_ha import ConfigEntry, ConfigEntryState  # noqa: E402

from custom_components.mc_overwatch import config_flow as cf  # noqa: E402

PASS = 0


def check(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1


class FakeEntries:
    def __init__(self):
        self.entries = {}
        self.reloaded = []

    def async_get_entry(self, eid):
        return self.entries.get(eid)

    def async_update_entry(self, entry, data=None, unique_id=None):
        if data is not None:
            entry.data = data
        if unique_id is not None:
            entry.unique_id = unique_id

    async def async_reload(self, eid):
        self.reloaded.append(eid)


class FakeHass:
    def __init__(self):
        self.config_entries = FakeEntries()


def make_flow(cls, entry_id):
    flow = cls()
    flow.hass = FakeHass()
    flow.context = {"entry_id": entry_id} if entry_id else {}
    flow.handler = entry_id
    return flow


def run(coro):
    return asyncio.run(coro)


async def ok_validate(hass, host, port, token):
    return None


async def bad_token(hass, host, port, token):
    from custom_components.mc_overwatch.coordinator import InvalidAuth

    raise InvalidAuth


def test_reauth_success_and_reload():
    entry = ConfigEntry(
        entry_id="e1",
        state=ConfigEntryState.SETUP_ERROR,
        data={"host": "1.2.3.4", "port": 8787, "token": "old"},
    )
    flow = make_flow(cf.McServerConfigFlow, "e1")
    flow.hass.config_entries.entries["e1"] = entry
    cf.validate_input, orig = ok_validate, cf.validate_input
    try:
        res = run(flow.async_step_reauth({}))
        check(
            res["type"] == "form" and res["step_id"] == "reauth_confirm",
            "reauth 首步出表单",
        )
        res = run(flow.async_step_reauth_confirm({"token": "new"}))
    finally:
        cf.validate_input = orig
    check(res == {"type": "abort", "reason": "reauth_successful"}, "reauth 成功 abort")
    check(entry.data["token"] == "new", "token 已写入 entry")
    check(flow.hass.config_entries.reloaded == ["e1"], "SETUP_ERROR 态显式 reload")


def test_reauth_no_assignment_to_readonly():
    global PASS
    flow = make_flow(cf.McServerConfigFlow, "e1")
    try:
        flow._reauth_entry_id = "x"  # HA ≥2024.12 会 AttributeError
        raise AssertionError("stub 应阻止 _reauth_entry_id 赋值（建模失真）")
    except AttributeError:
        PASS += 1


def test_reauth_bad_token():
    entry = ConfigEntry(
        entry_id="e1", data={"host": "1.2.3.4", "port": 8787, "token": "old"}
    )
    flow = make_flow(cf.McServerConfigFlow, "e1")
    flow.hass.config_entries.entries["e1"] = entry
    cf.validate_input, orig = bad_token, cf.validate_input
    try:
        res = run(flow.async_step_reauth_confirm({"token": "bad"}))
    finally:
        cf.validate_input = orig
    check(res["errors"] == {"base": "invalid_auth"}, "错误令牌出 invalid_auth")
    check(entry.data["token"] == "old", "entry 不被改动")


def test_options_current_prefers_options():
    entry = ConfigEntry(
        entry_id="e1", data={"scan_interval": 30}, options={"scan_interval": 60}
    )
    flow = make_flow(cf.McServerOptionsFlow, "e1")
    flow.hass.config_entries.entries["e1"] = entry
    form = run(flow.async_step_init(None))
    schema = form["data_schema"].schema
    key = next(k for k in schema if getattr(k, "key", None) == "scan_interval")
    check(key.default == 60, "表单当前值取 entry.options（而非 data 的 30）")
    res = run(flow.async_step_init({"scan_interval": 45}))
    check(
        res["type"] == "create_entry" and res["data"]["scan_interval"] == 45,
        "保存到 options",
    )
    check(flow.hass.config_entries.reloaded == [], "LOADED 态靠 listener reload")


def test_options_fallback_when_context_empty():
    entry = ConfigEntry(entry_id="e1", data={"scan_interval": 30}, options={})
    flow = make_flow(cf.McServerOptionsFlow, "e1")
    flow.context = {}  # 模拟 2024.8–2024.11：options context 不带 entry_id
    flow.hass.config_entries.entries["e1"] = entry
    form = run(flow.async_step_init(None))
    schema = form["data_schema"].schema
    key = next(k for k in schema if getattr(k, "key", None) == "scan_interval")
    check(key.default == 30, "经 handler 兜底拿到 entry，默认值=data 里的 30")


def test_reconfigure_migrates_unique_id():
    entry = ConfigEntry(
        entry_id="e1",
        unique_id="1.2.3.4:8787",
        data={"host": "1.2.3.4", "port": 8787, "token": "t", "scan_interval": 30},
    )
    flow = make_flow(cf.McServerConfigFlow, "e1")
    flow.hass.config_entries.entries["e1"] = entry
    cf.validate_input, orig = ok_validate, cf.validate_input
    try:
        res = run(
            flow.async_step_reconfigure(
                {"host": "http://5.6.7.8:9000/", "port": 8787, "token": "t2"}
            )
        )
    finally:
        cf.validate_input = orig
    check(
        res == {"type": "abort", "reason": "reconfigure_successful"}, "reconfigure 成功"
    )
    check(entry.unique_id == "5.6.7.8:9000", "unique_id 随新地址迁移")
    check(entry.data["port"] == 9000 and entry.data["token"] == "t2", "data 已更新")
    check(entry.data["scan_interval"] == 30, "scan_interval 保留不丢")
    check(flow.hass.config_entries.reloaded == [], "LOADED 态不双重 reload")


def test_reconfigure_not_loaded_reloads():
    entry = ConfigEntry(
        entry_id="e1",
        unique_id="1.2.3.4:8787",
        state=ConfigEntryState.SETUP_ERROR,
        data={"host": "1.2.3.4", "port": 8787, "token": "t"},
    )
    flow = make_flow(cf.McServerConfigFlow, "e1")
    flow.hass.config_entries.entries["e1"] = entry
    cf.validate_input, orig = ok_validate, cf.validate_input
    try:
        run(
            flow.async_step_reconfigure(
                {"host": "5.6.7.8", "port": 8787, "token": "t2"}
            )
        )
    finally:
        cf.validate_input = orig
    check(flow.hass.config_entries.reloaded == ["e1"], "未加载态修好后显式 reload")


def test_parse_error_no_crash():
    flow = make_flow(cf.McServerConfigFlow, None)
    res = run(
        flow.async_step_user(
            {"host": "192.168.1.5:abc", "port": 8787, "token": "t", "scan_interval": 30}
        )
    )
    check(
        res["type"] == "form" and res["errors"] == {"base": "cannot_connect"},
        "坏地址给表单错误而非异常",
    )


def test_user_success_parses_url():
    flow = make_flow(cf.McServerConfigFlow, None)
    cf.validate_input, orig = ok_validate, cf.validate_input
    try:
        res = run(
            flow.async_step_user(
                {
                    "host": "http://192.168.1.5:8787/status.json",
                    "port": 8787,
                    "token": "t",
                    "scan_interval": 30,
                }
            )
        )
    finally:
        cf.validate_input = orig
    check(res["type"] == "create_entry", "添加成功")
    check(
        res["data"]["host"] == "192.168.1.5" and res["data"]["port"] == 8787,
        "URL 形式解析出主机与端口",
    )


if __name__ == "__main__":
    for fn in (
        test_reauth_success_and_reload,
        test_reauth_no_assignment_to_readonly,
        test_reauth_bad_token,
        test_options_current_prefers_options,
        test_options_fallback_when_context_empty,
        test_reconfigure_migrates_unique_id,
        test_reconfigure_not_loaded_reloads,
        test_parse_error_no_crash,
        test_user_success_parses_url,
    ):
        fn()
        print(f"PASS {fn.__name__}")
    print(f"全部通过：{PASS} 项断言")

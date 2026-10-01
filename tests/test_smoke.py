#!/usr/bin/env python3
"""离线冒烟测试：不依赖 HA 环境（用 stub_ha.py 顶替 homeassistant 等依赖）。

覆盖：
1. 8 个实体能构造（历史上 P0：基类要求 key 而构造点没传 → 全灭）；
2. unique_id / 取值 / available 矩阵符合契约（离线→unavailable 等）；
3. 契约字段类型被污染时实体不炸（兜底为 None/不可用）；
4. config flow 的 host/port 解析与间隔校验。

运行：python3 tests/test_smoke.py（无第三方依赖）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import stub_ha  # noqa: F401  （必须先于集成模块 import）

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stub_ha import VolInvalid  # noqa: E402

from custom_components.mc_overwatch import binary_sensor, sensor  # noqa: E402
from custom_components.mc_overwatch.config_flow import (  # noqa: E402
    SCAN_INTERVAL_SCHEMA,
    parse_host_port,
)
from custom_components.mc_overwatch.const import (  # noqa: E402
    KEY_CPU,
    KEY_MEMORY,
    KEY_MSPT,
    KEY_ONLINE,
    KEY_PLAYERS,
    KEY_TPS,
    KEY_UPTIME,
    KEY_VERDICT,
)
from custom_components.mc_overwatch.entity import as_dict, as_num  # noqa: E402

PASS = 0


def check(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1


class FakeCoordinator:
    def __init__(self, data, ok=True):
        self.data = data
        self.last_update_success = ok


class FakeEntry:
    entry_id = "ent123"
    data = {}
    options = {}


# 契约 §3 示例快照（含新 ready 字段）
SAMPLE = {
    "ts": 1769500000,
    "server": {
        "online": True,
        "process_alive": True,
        "port_open": True,
        "ready": True,
        "pid": 82122,
        "uptime_s": 123456,
        "started_at": "2026-09-27T16:11:00+08:00",
        "rss_bytes": 8262541312,
        "cpu_percent": 12.3,
    },
    "rcon": {
        "ok": True,
        "error": None,
        "players": {"online": 2, "max": 20, "names": ["AluneMua", "Steve"]},
        "tps": {
            "mspt": 2.3,
            "tps_1m": 19.98,
            "tps_5m": 19.98,
            "tps_15m": 19.97,
            "raw": "Average time per tick: 2.3ms",
        },
    },
    "monitor": {
        "verdict": "OK",
        "checked_at": "2026-09-27 18:07:03",
        "events_1h": {"oom": 0, "fatal": 0, "lag": 3, "unload_wait": 0},
        "chunky": "42.1%",
    },
    "exporter": {"version": "1.1.0", "interval_s": 15},
}


def build(coordinator):
    entry = FakeEntry()
    ents = [binary_sensor.OnlineBinarySensor(coordinator, entry, KEY_ONLINE)]
    for cls, key in [
        (sensor.PlayersSensor, KEY_PLAYERS),
        (sensor.TpsSensor, KEY_TPS),
        (sensor.MsptSensor, KEY_MSPT),
        (sensor.MemorySensor, KEY_MEMORY),
        (sensor.CpuSensor, KEY_CPU),
        (sensor.UptimeSensor, KEY_UPTIME),
        (sensor.VerdictSensor, KEY_VERDICT),
    ]:
        ents.append(cls(coordinator, entry, key))
    return ents


def by_key(ents):
    return {e._key: e for e in ents}


def test_construct_and_values():
    ents = build(FakeCoordinator(SAMPLE))
    check(len(ents) == 8, "应有 8 个实体")
    e = by_key(ents)
    for k in (
        KEY_ONLINE,
        KEY_PLAYERS,
        KEY_TPS,
        KEY_MSPT,
        KEY_MEMORY,
        KEY_CPU,
        KEY_UPTIME,
        KEY_VERDICT,
    ):
        check(e[k]._attr_unique_id == f"ent123-{k}", f"unique_id 格式: {k}")
    check(e[KEY_ONLINE].is_on is True, "online=True")
    check(e[KEY_PLAYERS].native_value == 2, "玩家数")
    check(
        e[KEY_PLAYERS].extra_state_attributes["names"] == ["AluneMua", "Steve"], "名单"
    )
    check(e[KEY_TPS].native_value == 19.98, "TPS")
    check(e[KEY_MSPT].native_value == 2.3, "MSPT")
    check(e[KEY_MEMORY].native_value == round(8262541312 / 1024**3, 2), "内存 GB")
    check(e[KEY_CPU].native_value == 12.3, "CPU")
    check(e[KEY_UPTIME].native_value is not None, "运行时长可解析")
    check(e[KEY_VERDICT].native_value == "OK", "判级")
    check(all(x.available for x in ents), "全量在线时全部可用")


def test_availability_matrix():
    offline = dict(SAMPLE, server={**SAMPLE["server"], "online": False})
    e2 = by_key(build(FakeCoordinator(offline)))
    check(e2[KEY_ONLINE].is_on is False, "offline → online off")
    check(not e2[KEY_PLAYERS].available, "offline → players unavailable")
    check(not e2[KEY_TPS].available, "offline → tps unavailable")
    check(not e2[KEY_MEMORY].available, "offline → memory unavailable")
    check(e2[KEY_ONLINE].available, "offline 但导出器正常 → online 仍可用")

    rcon_down = dict(
        SAMPLE, rcon={"ok": False, "error": "refused", "players": None, "tps": None}
    )
    e3 = by_key(build(FakeCoordinator(rcon_down)))
    check(not e3[KEY_PLAYERS].available, "rcon 不通 → players unavailable")
    check(e3[KEY_MEMORY].available, "rcon 不通不影响 memory")

    e4 = by_key(build(FakeCoordinator(dict(SAMPLE, monitor=None))))
    check(not e4[KEY_VERDICT].available, "monitor 缺失 → verdict unavailable")

    e5 = by_key(build(FakeCoordinator(SAMPLE, ok=False)))
    check(not e5[KEY_ONLINE].available, "导出器不可达 → 全部 unavailable")


def test_garbage_payload_no_crash():
    bad = {"server": "x", "rcon": [1, 2], "monitor": 7}
    ents = build(FakeCoordinator(bad))
    e = by_key(ents)
    check(not e[KEY_ONLINE].is_on, "server 类型污染 → online off")
    check(not e[KEY_PLAYERS].available, "rcon 类型污染 → players unavailable")
    garbage = dict(
        SAMPLE,
        server={
            **SAMPLE["server"],
            "rss_bytes": "8G",
            "cpu_percent": "12%",
            "uptime_s": "x",
        },
    )
    e2 = by_key(build(FakeCoordinator(garbage)))
    check(e2[KEY_MEMORY].native_value is None, "rss 类型污染 → None 不炸")
    check(e2[KEY_CPU].native_value is None, "cpu 类型污染 → None 不炸")
    check(as_num(True) is None, "bool 不算数值")


def test_parse_host_port():
    cases = [
        ("192.168.1.5", 8787, ("192.168.1.5", 8787)),
        ("http://192.168.1.5:8787/status.json", 8787, ("192.168.1.5", 8787)),
        ("192.168.1.5:9000", 8787, ("192.168.1.5", 9000)),
        ("mc.local/", 8787, ("mc.local", 8787)),
        ("https://mc.local:9000", 8787, ("mc.local", 9000)),
    ]
    for raw, port, want in cases:
        check(parse_host_port(raw, port) == want, f"parse_host_port({raw!r})")


def test_scan_interval_schema():
    global PASS
    check(SCAN_INTERVAL_SCHEMA(30) == 30, "正常值")
    check(SCAN_INTERVAL_SCHEMA("15") == 15, "字符串数字可转换")
    for bad in (0, -1, 1000000):
        try:
            SCAN_INTERVAL_SCHEMA(bad)
            raise AssertionError(f"{bad} 应被拒绝")
        except VolInvalid:
            PASS += 1


def test_helpers():
    check(as_dict(None) == {}, "as_dict(None)")
    check(as_num("12") is None, "as_num(str)")


if __name__ == "__main__":
    for fn in (
        test_construct_and_values,
        test_availability_matrix,
        test_garbage_payload_no_crash,
        test_parse_host_port,
        test_scan_interval_schema,
        test_helpers,
    ):
        fn()
        print(f"PASS {fn.__name__}")
    print(f"全部通过：{PASS} 项断言")

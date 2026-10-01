#!/usr/bin/env python3
"""exporter.py 核心分支的离线测试（不需要真实 MC 服务器）。

覆盖 2026-09-28 修复的关键行为：
- slp_ready：真握手 / 立即断开 / 拒绝连接 三态
- rcon_collect：逐条容错（list 成功 tick 挂 → players 保留 tps=null）
- build_snapshot：online=ready 语义（SLP 通即在线，与进程检测解耦）

运行：python3 tests/test_exporter.py
"""

import contextlib
import importlib.util
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
EXP = os.path.join(HERE, "..", "macapp", "exporter", "exporter.py")
sys.path.insert(0, os.path.dirname(os.path.abspath(EXP)))
spec = importlib.util.spec_from_file_location("exporter_mod", EXP)
ex = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ex)

PASS = 0


def check(cond, msg):
    global PASS
    assert cond, msg
    PASS += 1


def fake_slp_server(port, drop_immediately=False):
    """起一个一次性假 SLP 监听：正常回 100 字节 / 或者接受后立即断开。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(1)
    stop = threading.Event()

    def serve():
        srv.settimeout(5)
        try:
            conn, _ = srv.accept()
            if drop_immediately:
                conn.close()
            else:
                conn.settimeout(2)
                with contextlib.suppress(OSError):
                    conn.recv(4096)
                conn.sendall(b"x" * 100)
                conn.close()
        except OSError:
            pass
        finally:
            stop.set()
            srv.close()

    threading.Thread(target=serve, daemon=True).start()
    time.sleep(0.05)
    return stop


def test_slp_ready():
    port = 45671
    stop = fake_slp_server(port)
    check(ex.slp_ready(port, timeout=2.0) is True, "正常 SLP 应答 → ready")
    stop.wait(3)

    port = 45672
    stop = fake_slp_server(port, drop_immediately=True)
    check(ex.slp_ready(port, timeout=2.0) is False, "接受后立即断开 → not ready")
    stop.wait(3)

    check(ex.slp_ready(45673, timeout=1.0) is False, "无人监听 → not ready")


class FakeRcon:
    """可编程假 RCON：按 dict 返回回显或抛异常。"""

    behavior = {}

    def __init__(self, *a, **k):
        pass

    def cmd(self, command, timeout=10):
        b = FakeRcon.behavior
        if command.startswith("list"):
            v = b.get("list", "There are 2 of a max of 20 players online: Steve, Alex")
        elif command.startswith("tick"):
            v = b.get(
                "tick",
                "The game is running normally\n"
                "Target tick rate: 20.0 per second.\n"
                "Average time per tick: 2.3ms (Target: 50.0ms)\n"
                "Percentiles: P50: 1.5ms P95: 1.8ms P99: 2.3ms, sample: 100",
            )
        else:
            v = ""
        if isinstance(v, Exception):
            raise v
        return v

    def close(self):
        pass


def with_fake_rcon(behavior, fn):
    FakeRcon.behavior = behavior
    orig = ex.Rcon
    ex.Rcon = FakeRcon
    try:
        return fn()
    finally:
        ex.Rcon = orig


def test_rcon_collect():
    out = with_fake_rcon({}, lambda: ex.rcon_collect({"rcon_password": "x"}))
    check(out["ok"] is True, "正常：ok")
    check(
        out["players"]["online"] == 2 and out["players"]["names"] == ["Steve", "Alex"],
        "正常：players",
    )
    check(
        out["tps"]["mspt"] == 2.3 and out["tps"]["tps_5m"] == 20.0,
        "正常：mspt→tps 封顶 20",
    )
    check(out["error"] is None, "正常：无错误")

    out = with_fake_rcon(
        {"tick": OSError("connection reset")},
        lambda: ex.rcon_collect({"rcon_password": "x"}),
    )
    check(out["ok"] is True, "tick 挂但 list 成功 → ok 仍 True")
    check(
        out["players"] is not None and out["players"]["online"] == 2, "快照保留 players"
    )
    check(out["tps"] is None, "命令异常 → tps=null（契约）")
    check(out["error"] and "tick query" in out["error"], "error 留因")

    out = with_fake_rcon(
        {"list": OSError("broken pipe")},
        lambda: ex.rcon_collect({"rcon_password": "x"}),
    )
    check(out["ok"] is False and out["players"] is None, "list 挂 → ok=False")
    check(out["error"] and "list" in out["error"], "error 留因")

    out = with_fake_rcon(
        {"list": "some garbage reply"}, lambda: ex.rcon_collect({"rcon_password": "x"})
    )
    check(
        out["ok"] is False and "unexpected list response" in (out["error"] or ""),
        "list 异常回显留痕",
    )

    out = with_fake_rcon(
        {"tick": "Percentiles: P50: 1.5ms"},
        lambda: ex.rcon_collect({"rcon_password": "x"}),
    )
    check(
        out["tps"]["mspt"] is None and "Percentiles" in out["tps"]["raw"],
        "解析失败 → 数值 null、raw 保留（契约）",
    )


def test_snapshot_online_semantics():
    orig = (ex.get_pid, ex.ps_stats, ex.rcon_collect, ex.port_open, ex.slp_ready)
    try:
        ex.get_pid = lambda: None
        ex.rcon_collect = lambda cfg: {
            "ok": False,
            "error": "refused",
            "players": None,
            "tps": None,
        }
        ex.port_open = lambda p, timeout=2.0: True
        ex.slp_ready = lambda p, timeout=3.0: True
        snap = ex.build_snapshot({"mc_port": 25565}, "/nonexistent")
        check(
            snap["server"]["online"] is True
            and snap["server"]["process_alive"] is False,
            "online=ready 与进程检测解耦：SLP 通即在线",
        )
        check(
            snap["server"]["ready"] is True and snap["server"]["port_open"] is True,
            "ready/port_open 字段",
        )
        check(
            snap["rcon"]["ok"] is False and snap["monitor"] is None, "rcon/monitor 容错"
        )

        ex.slp_ready = lambda p, timeout=3.0: False
        snap = ex.build_snapshot({"mc_port": 25565}, "/nonexistent")
        check(
            snap["server"]["online"] is False and snap["server"]["port_open"] is True,
            "端口通但 SLP 不过（启动中/后端未就绪）→ 离线",
        )
    finally:
        ex.get_pid, ex.ps_stats, ex.rcon_collect, ex.port_open, ex.slp_ready = orig


if __name__ == "__main__":
    test_slp_ready()
    print("PASS test_slp_ready")
    test_rcon_collect()
    print("PASS test_rcon_collect")
    test_snapshot_online_semantics()
    print("PASS test_snapshot_online_semantics")
    print(f"全部通过：{PASS} 项断言")

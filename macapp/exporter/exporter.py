#!/usr/bin/env python3
"""mc-ha-exporter — MC 服务器状态导出器（供 Home Assistant 轮询）。

用法: python3 exporter.py <config.json 路径>
      python3 exporter.py --selftest   （解析器自测，无需 config）
纯标准库。后台线程每 refresh_interval_s 刷新一次快照；HTTP 请求只读缓存，
绝不在请求线程里采集（服务器卡死时 RCON 慢也不影响 HA 拉取）。
数据契约见仓库 README / mc-ha-integration-plan.md §3；服务器离线是正常态。
"""

import contextlib
import json
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.1.1"


# ---------------------------------------------------------------- RCON 客户端
class Rcon:
    """Source RCON（原版协议），移植自 seedroll/rconclient.py，超时可配。"""

    def __init__(self, host, port, password, timeout=10):
        self.s = socket.create_connection((host, port), timeout=timeout)
        self._send(1, 3, password)
        resp = self._recv()
        if resp is None:
            raise RuntimeError("rcon auth failed (no response)")
        rid = struct.unpack("<ii", resp[:8])[0]
        if rid == -1:
            raise RuntimeError("rcon auth failed (wrong password)")

    def _send(self, i, t, payload):
        body = struct.pack("<ii", i, t) + payload.encode("utf8") + b"\x00\x00"
        self.s.sendall(struct.pack("<i", len(body)) + body)

    def _recv(self):
        try:
            header = b""
            while len(header) < 4:
                chunk = self.s.recv(4 - len(header))
                if not chunk:
                    return None
                header += chunk
            ln = struct.unpack("<i", header)[0]
            data = b""
            while len(data) < ln:
                chunk = self.s.recv(ln - len(data))
                if not chunk:
                    return None
                data += chunk
            return data
        except TimeoutError:
            return None

    def cmd(self, command, timeout=10):
        self.s.settimeout(timeout)
        self._send(int(time.time()) & 0x7FFFFFFF or 7, 2, command)
        resp = self._recv()
        if resp is None:
            return ""
        return resp[8:-2].decode("utf8", "replace").strip()

    def close(self):
        with contextlib.suppress(Exception):
            self.s.close()


# ---------------------------------------------------------------- 解析器
RE_LIST = re.compile(r"There are (\d+) of a max of (\d+) players online(?::\s*(.+))?")
# tick query 实测回显（1.21.1，2026-09-28）：spark 经 RCON 无回显，不可用
#   The game is running normally
#   Target tick rate: 20.0 per second.
#   Average time per tick: 1.5ms (Target: 50.0ms)
#   Percentiles: P50: 1.5ms P95: 1.8ms P99: 2.3ms, sample: 100
# 注意 [^\d] 段不允许跨越数字，天然锁回第一个紧跟 avg/average/mean 的数值，
# 不会被后面的 Percentiles 数值带偏。
RE_TICK_AVG = re.compile(
    r"\b(?:avg|average|mean)\b[^\d]{0,40}?([\d.]+)\s*ms"
    r"|([\d.]+)\s*ms[^\d]{0,10}\b(?:avg|average)\b",
    re.I,
)


def strip_colors(s):
    return re.sub(r"§.", "", s or "")


def parse_mspt(text):
    if not text:
        return None
    m = RE_TICK_AVG.search(text)
    if not m:
        return None
    try:
        return float(next(g for g in m.groups() if g is not None))
    except (StopIteration, ValueError):
        return None


def parse_etime(s):
    """ps etime（[[dd-]hh:]mm:ss）→ 秒。"""
    days = 0
    if "-" in s:
        days, s = s.split("-", 1)
        days = int(days)
    parts = [int(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    h, m, sec = parts[-3:]
    return days * 86400 + h * 3600 + m * 60 + sec


# ---------------------------------------------------------------- 探活
def _varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out += bytes([b | 0x80])
        else:
            return out + bytes([b])


def port_open(port, timeout=2.0):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def slp_ready(port, timeout=3.0):
    """对 127.0.0.1:port 发 Server List Ping 握手，收到响应才算「真可进服」。

    裸 TCP connect 只能证明 ZstdNet 代理在 accept（它先于 MC 后端起来），
    证明不了游戏服务就绪；SLP 会一路穿透代理到后端（2026-09-28 实测可行）。"""
    host = "127.0.0.1"
    try:
        hs = (
            _varint(767)
            + _varint(len(host))
            + host.encode()
            + struct.pack(">H", port)
            + _varint(1)
        )
        body = _varint(0) + hs
        s = socket.create_connection((host, port), timeout=timeout)
        s.sendall(_varint(len(body)) + body + b"\x01\x00")
        s.settimeout(timeout)
        data = b""
        while len(data) < 64:
            c = s.recv(4096)
            if not c:
                break
            data += c
        s.close()
        return len(data) > 0
    except OSError:
        return False


# ---------------------------------------------------------------- 采集器
def get_pid():
    """精确找 MC 服务器 java 进程（check.py 同款：pgrep -x java + 命令行验证）。
    不能用 pgrep -f 'unix_args.txt'——运维命令文本含该词会误判。"""
    r = subprocess.run(["pgrep", "-x", "java"], capture_output=True, text=True)
    for p in r.stdout.split():
        c = subprocess.run(
            ["ps", "-p", p, "-o", "command="], capture_output=True, text=True
        ).stdout
        if "unix_args" in c:
            return int(p)
    return None


def ps_stats(pid):
    r = subprocess.run(
        ["ps", "-o", "rss=,pcpu=,etime=", "-p", str(pid)],
        capture_output=True,
        text=True,
    )
    parts = r.stdout.split()
    if len(parts) < 3:
        return None, None, None
    try:
        return int(parts[0]) * 1024, float(parts[1]), parse_etime(parts[-1])
    except ValueError:
        return None, None, None


def rcon_collect(cfg):
    """RCON 采集。每条命令独立容错：list 失败不影响整份快照丢光，
    只降级对应字段并在 error 里留因。"""
    out = {"ok": False, "error": None, "players": None, "tps": None}
    timeout = int(cfg.get("rcon_timeout_s", 10))
    try:
        rc = Rcon(
            cfg.get("rcon_host", "127.0.0.1"),
            int(cfg.get("rcon_port", 25575)),
            cfg.get("rcon_password", ""),
            timeout,
        )
    except Exception as e:
        out["error"] = str(e)
        return out
    try:
        list_raw = None
        try:
            list_raw = rc.cmd("list", timeout)
        except Exception as e:
            out["error"] = f"list: {e}"
        m = RE_LIST.search(list_raw) if list_raw else None
        if m:
            names = (
                [n.strip() for n in m.group(3).split(",") if n.strip()]
                if m.group(3)
                else []
            )
            out["players"] = {
                "online": int(m.group(1)),
                "max": int(m.group(2)),
                "names": names,
            }
            out["ok"] = True
        elif list_raw is not None:
            out["error"] = f"unexpected list response: {list_raw[:120]!r}"

        # TPS/MSPT 只走原生 tick query（spark 经 RCON 无回显，2026-09-28 实测）
        # 契约：命令异常 → tps=null；命令成功但解析失败 → 数值 null、raw 保留
        try:
            tick_raw = strip_colors(rc.cmd("tick query", timeout))
        except Exception as e:
            if out["error"] is None:
                out["error"] = f"tick query: {e}"
            tick_raw = None
        if tick_raw is not None:
            mspt = parse_mspt(tick_raw)
            tps = round(min(20.0, 1000.0 / mspt), 2) if mspt and mspt > 0 else None
            out["tps"] = {
                "mspt": mspt,
                "tps_1m": tps,
                "tps_5m": tps,
                "tps_15m": tps,
                "raw": tick_raw[:400],
            }
        return out
    finally:
        rc.close()


def load_last_check(cfg_dir):
    try:
        with open(os.path.join(cfg_dir, "last_check.json"), encoding="utf8") as f:
            return json.load(f)
    except Exception:
        return None


def build_snapshot(cfg, cfg_dir):
    pid = get_pid()
    server = {
        "online": False,
        "process_alive": pid is not None,
        "port_open": False,
        "ready": False,
        "pid": pid,
        "uptime_s": None,
        "started_at": None,
        "rss_bytes": None,
        "cpu_percent": None,
    }
    if pid is not None:
        rss, cpu, up = ps_stats(pid)
        server["rss_bytes"], server["cpu_percent"], server["uptime_s"] = rss, cpu, up
        if up:
            started = datetime.now().astimezone() - timedelta(seconds=up)
            server["started_at"] = started.isoformat(timespec="seconds")
    mc_port = int(cfg.get("mc_port", 25565))
    # 探活与进程状态解耦：SLP 通即「可进服」（进程检测不到也认，防误判）。
    server["port_open"] = port_open(mc_port)
    server["ready"] = slp_ready(mc_port) if server["port_open"] else False
    server["online"] = server["ready"]
    return {
        "ts": int(time.time()),
        "server": server,
        "rcon": rcon_collect(cfg),
        "monitor": load_last_check(cfg_dir),
        "exporter": {
            "version": VERSION,
            "interval_s": int(cfg.get("refresh_interval_s", 15)),
        },
    }


# ---------------------------------------------------------------- 自测
def selftest():
    assert parse_mspt("Average time per tick: 1.5ms (Target: 50.0ms)") == 1.5
    assert parse_mspt("tick time 12.5ms average") == 12.5
    assert parse_mspt("mean tick: 3.25 ms") == 3.25
    assert parse_mspt("The game is running normally") is None
    assert parse_mspt("Target tick rate: 20.0 per second.") is None
    full = (
        "The game is running normally\nTarget tick rate: 20.0 per second.\n"
        "Average time per tick: 2.3ms (Target: 50.0ms)\n"
        "Percentiles: P50: 1.5ms P95: 1.8ms P99: 2.3ms, sample: 100"
    )
    assert parse_mspt(full) == 2.3, "必须取 Average 行而非 Percentiles"
    assert parse_mspt("") is None

    assert parse_etime("02:01:10") == 7270
    assert parse_etime("45:10") == 2710
    assert parse_etime("3-05:02:01") == 3 * 86400 + 5 * 3600 + 2 * 60 + 1

    m = RE_LIST.search("There are 0 of a max of 20 players online")
    assert m and m.group(1) == "0" and m.group(3) is None
    m = RE_LIST.search("There are 2 of a max of 20 players online: Steve, Alex")
    assert m and [n.strip() for n in m.group(3).split(",")] == ["Steve", "Alex"]

    assert _varint(767) == b"\xff\x05"
    assert _varint(0) == b"\x00"
    print(f"selftest OK ({VERSION})")


# ---------------------------------------------------------------- 服务
class Exporter:
    def __init__(self, cfg, cfg_dir):
        self.cfg = cfg
        self.cfg_dir = cfg_dir
        self._lock = threading.Lock()
        self._snap = None

    def refresh_loop(self):
        while True:
            self.refresh()
            time.sleep(max(5, int(self.cfg.get("refresh_interval_s", 15))))

    def refresh(self):
        try:
            snap = build_snapshot(self.cfg, self.cfg_dir)
            with self._lock:
                self._snap = snap
        except Exception:
            import traceback

            traceback.print_exc()

    def snapshot(self):
        with self._lock:
            if self._snap is not None:
                return self._snap
        return {
            "ts": None,
            "note": "first refresh in progress",
            "server": {"online": False},
            "rcon": {"ok": False},
            "monitor": None,
            "exporter": {
                "version": VERSION,
                "interval_s": int(self.cfg.get("refresh_interval_s", 15)),
            },
        }


def make_handler(app):
    token = str(app.cfg.get("token", ""))

    class Handler(BaseHTTPRequestHandler):
        server_version = f"mc-ha-exporter/{VERSION}"

        def do_GET(self):
            u = urlparse(self.path)
            if u.path not in ("/status.json", "/"):
                self._json(404, {"error": "not found"})
                return
            q = parse_qs(u.query)
            given = q.get("token", [""])[0] or (
                self.headers.get("X-Export-Token") or ""
            )
            if not token or given != token:
                self._json(403, {"error": "forbidden"})
                return
            self._json(200, app.snapshot())

        def _json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    return Handler


def truncate_old_logs(cfg_dir, limit=2 * 1024 * 1024):
    for name in ("exporter.log", "exporter.err.log"):
        p = os.path.join(cfg_dir, name)
        try:
            if os.path.getsize(p) > limit:
                open(p, "w").close()
        except OSError:
            pass


def parent_watchdog():
    """父进程（监控 App）死亡 → 工作进程随之自退。保证"数据服务随 App 存亡"，
    不留孤儿占 8787（orphan 的 getppid 会变成 1）。"""
    time.sleep(5)
    while True:
        if os.getppid() == 1:
            os._exit(0)
        time.sleep(5)


def main():
    if "--selftest" in sys.argv:
        selftest()
        return
    cfg_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    )
    with open(cfg_path, encoding="utf8") as f:
        cfg = json.load(f)
    cfg_dir = os.path.dirname(os.path.abspath(cfg_path))
    truncate_old_logs(cfg_dir)
    bind = cfg.get("bind", "0.0.0.0")
    port = int(cfg.get("http_port", 8787))
    app = Exporter(cfg, cfg_dir)
    try:
        # 先 bind 再起采集线程：端口占用时立即失败退出并给出明确指引
        srv = ThreadingHTTPServer((bind, port), make_handler(app))
    except OSError as e:
        if e.errno == 48:  # EADDRINUSE
            print(
                f"端口 {port} 已被占用：可能已有工作进程在跑，或被其它程序占用。"
                f"排查：lsof -nP -i :{port}。"
                "监管 App 会自动重生工作进程，勿需人工拉起。",
                flush=True,
            )
        raise
    threading.Thread(target=app.refresh_loop, daemon=True).start()
    threading.Thread(target=parent_watchdog, daemon=True).start()
    print(
        f"{time.strftime('%F %T')} mc-ha-exporter {VERSION} "
        f"listening on {bind}:{port}, refresh {cfg.get('refresh_interval_s', 15)}s",
        flush=True,
    )
    srv.serve_forever()


if __name__ == "__main__":
    main()

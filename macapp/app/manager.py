#!/usr/bin/env python3
"""MC服务器监控 App 的安装器/自愈工具（被 App 内部调用，也可 CLI 使用）。

架构（2026-09-28 整合）：菜单栏 App 是唯一常驻实体——launchd 代理 mc.ha-exporter
直接拉起 App 本体（RunAtLoad + 崩溃自愈，正常退出不拉起），App 内部再监管 python
采集工作进程（无独立生命周期，随 App 存亡）。本体必须部署到 ~/Library：launchd
用户代理无权读写 ~/Documents（TCC 静默拦截），从 Documents 跑常驻二进制必踩坑。

双击 ~/Documents/MC/mc-ha-exporter.app = 安装/更新副本 → 部署本体到 ~/Library →
激活 launchd 代理 → 退出（本体接管，菜单栏出图标）。

CLI: manager.py --install | --ensure | --status | --stop | --restart
"""
import io
import json
import os
import plistlib
import secrets
import shutil
import subprocess
import sys
import time
import urllib.request

RES = os.path.dirname(os.path.abspath(__file__))            # …/Contents/Resources
# 注意是两层 dirname：Resources → Contents → *.app 根（少一层会拍平整个包）
APP_BUNDLE = os.path.dirname(os.path.dirname(RES))
HOME = os.path.expanduser("~")
WORK = os.path.join(HOME, "Library", "mc-ha-exporter")
DEPLOYED_APP = os.path.join(WORK, "mc-ha-exporter.app")      # 常驻本体位置（~/Library 防TCC）
PLIST_PATH = os.path.join(HOME, "Library", "LaunchAgents", "mc.ha-exporter.plist")
LABEL = "mc.ha-exporter"
SERVER_PROPS = os.path.join(HOME, "Documents", "MC", "Server", "server.properties")

# 本体内部需要保持一致的关键文件（相对 .app 根）
APP_FILES = ("Contents/Info.plist",
             "Contents/MacOS/mc-ha-exporter",
             "Contents/Resources/exporter.py",
             "Contents/Resources/manager.py")

DEFAULT_CFG = {
    "http_port": 8787,
    "bind": "0.0.0.0",
    "token": "",
    "mc_port": 25565,
    "rcon_host": "127.0.0.1",
    "rcon_port": 25575,
    "rcon_password": "",
    "rcon_timeout_s": 10,
    "refresh_interval_s": 15,
}


def pick_python():
    for p in ("/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(p):
            return p
    return "python3"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def files_equal(a, b):
    try:
        return open(a, "rb").read() == open(b, "rb").read()
    except OSError:
        return False


# ---------------------------------------------------------------- 安装
def read_rcon_password():
    """返回 (password|None, err|None)。None 表示读不到文件（TCC/路径）。"""
    try:
        with open(SERVER_PROPS, encoding="utf8", errors="replace") as f:
            for line in f:
                if line.startswith("rcon.password="):
                    return line.split("=", 1)[1].strip(), None
        return "", "server.properties 里没有 rcon.password（RCON 未开启？）"
    except OSError as e:
        return None, f"读不了 server.properties（{e.__class__.__name__}），" \
                     "需要在弹出的授权里允许访问「文稿」文件夹"


def load_cfg():
    try:
        with open(os.path.join(WORK, "config.json"), encoding="utf8") as f:
            cfg = json.load(f)
    except Exception:
        cfg = dict(DEFAULT_CFG)
    changed = False
    for k, v in DEFAULT_CFG.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if not cfg.get("token"):
        cfg["token"] = secrets.token_hex(16)
        changed = True
    return cfg, changed


def save_cfg(cfg):
    p = os.path.join(WORK, "config.json")
    with open(p, "w", encoding="utf8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.chmod(p, 0o600)


def deploy_app_bundle():
    """把当前 .app 部署为常驻本体（~/Library/mc-ha-exporter/）。
    返回 changed。本体自身运行时（APP_BUNDLE == DEPLOYED_APP）恒 False——
    绝不能 rmtree 自己：半包状态会自我删除后复制失败，launchd 再也拉不起来。"""
    if os.path.realpath(APP_BUNDLE) == os.path.realpath(DEPLOYED_APP):
        return False
    changed = any(not files_equal(os.path.join(APP_BUNDLE, rel),
                                  os.path.join(DEPLOYED_APP, rel))
                  for rel in APP_FILES)
    if changed:
        # 拷到临时目录再原子换名：中断不会留下半包
        tmp = DEPLOYED_APP + ".tmp"
        if os.path.exists(tmp):
            shutil.rmtree(tmp)
        shutil.copytree(APP_BUNDLE, tmp)
        if os.path.exists(DEPLOYED_APP):
            shutil.rmtree(DEPLOYED_APP)
        os.replace(tmp, DEPLOYED_APP)
    return changed


def deploy_worker():
    src = os.path.join(RES, "exporter.py")
    dst = os.path.join(WORK, "exporter.py")
    changed = not files_equal(src, dst)
    if changed:
        shutil.copyfile(src, dst)
    return changed


def make_plist_bytes():
    data = {
        "Label": LABEL,
        "ProgramArguments": [os.path.join(DEPLOYED_APP, "Contents", "MacOS",
                                          "mc-ha-exporter")],
        "RunAtLoad": True,
        # 正常退出（用户主动退出）不拉起；崩溃/被杀才自愈重启
        "KeepAlive": {"SuccessfulExit": False},
        "StandardOutPath": os.path.join(WORK, "app.log"),
        "StandardErrorPath": os.path.join(WORK, "app.err.log"),
    }
    bio = io.BytesIO()
    plistlib.dump(data, bio)
    return bio.getvalue()


def agent_loaded():
    return run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"]).returncode == 0


def agent_pid():
    r = run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"])
    if r.returncode != 0:
        return None
    for line in r.stdout.splitlines():
        s = line.strip()
        if s.startswith("pid ="):
            try:
                return int(s.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None


def agent_bootstrap():
    last = ""
    for _ in range(3):
        r = run(["launchctl", "bootstrap", f"gui/{os.getuid()}", PLIST_PATH])
        if r.returncode == 0 or "already bootstrapped" in (r.stderr + r.stdout):
            return
        last = (r.stderr or r.stdout).strip()
        time.sleep(2)     # bootout 是异步的，立即 bootstrap 可能撞竞态
    raise RuntimeError(f"launchctl bootstrap 失败: {last}")


def agent_stop():
    run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"])
    for _ in range(10):   # 等 job 真正卸载（bootout 异步），最多 10s
        if not agent_loaded():
            return
        time.sleep(1)


PLIST_PENDING = os.path.join(WORK, ".plist_reload_pending")


def ensure_install(force_restart=False):
    """部署/更新全部文件并按需激活 launchd 代理。返回 (notes, warns)。

    代理规则：本体/配置有更新 → bootout 后重新 bootstrap（launchd 不为已加载
    job 重读 plist，只 kickstart 无效）；仅当代理进程缺失时 kickstart 拉起；
    调用者就是代理本体（getppid==代理pid）时绝不动代理——那是自杀。此时若确有
    待重载项，落 pending 标记，由下一次非本体调用消费。"""
    notes, warns = [], []
    os.makedirs(WORK, exist_ok=True)

    worker_changed = deploy_worker()
    if worker_changed:
        notes.append("采集工作进程已部署/更新")

    cfg, cfg_changed = load_cfg()
    pw, pw_err = read_rcon_password()
    if pw is not None:
        if cfg.get("rcon_password") != pw:
            cfg["rcon_password"] = pw
            cfg_changed = True
            notes.append("RCON 密码已从 server.properties 同步")
    elif pw_err:
        warns.append(pw_err + "；沿用已存配置")
    if cfg_changed or not os.path.exists(os.path.join(WORK, "config.json")):
        save_cfg(cfg)
        notes.append("config.json 已写入")

    app_changed = deploy_app_bundle()
    if app_changed:
        notes.append("App 本体已部署到 ~/Library/mc-ha-exporter/")

    plist_bytes = make_plist_bytes()
    plist_changed = True
    try:
        plist_changed = open(PLIST_PATH, "rb").read() != plist_bytes
    except OSError:
        pass
    if plist_changed:
        os.makedirs(os.path.dirname(PLIST_PATH), exist_ok=True)
        with open(PLIST_PATH, "wb") as f:
            f.write(plist_bytes)
        notes.append("launchd plist 已写入")

    pending = os.path.exists(PLIST_PENDING)
    need_reload = force_restart or plist_changed or app_changed or pending
    loaded = agent_loaded()
    pid = agent_pid()
    self_hosted = (pid == os.getppid())
    if loaded and need_reload and not self_hosted:
        agent_stop()
        loaded = False
        if pending:
            try:
                os.remove(PLIST_PENDING)
            except OSError:
                pass
        notes.append("代理已重载（本体/配置更新生效）")
    elif loaded and need_reload and self_hosted:
        open(PLIST_PENDING, "w").close()   # 本体自己动不了自己，挂起给下次
        warns.append("本体/配置有更新，将在下次由安装副本或 CLI 触发重载")
    if not loaded:
        agent_bootstrap()
        notes.append("launchd 代理已注册/启动（App 常驻）")
    elif agent_pid() is None:
        run(["launchctl", "kickstart", f"gui/{os.getuid()}/{LABEL}"])
        notes.append("代理进程缺失，已拉起")
    return notes, warns


def wait_exporter(cfg, timeout=8):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{int(cfg.get('http_port', 8787))}/status.json"
                f"?token={cfg.get('token', '')}")
            with urllib.request.urlopen(req, timeout=3) as r:
                json.load(r)
            return True
        except Exception:
            time.sleep(1)
    return False


# ---------------------------------------------------------------- 状态
def lan_ip():
    for iface in ("en0", "en1", "en2"):
        ip = run(["ipconfig", "getifaddr", iface]).stdout.strip()
        if ip:
            return ip
    return "？"


def human_uptime(s):
    if not s:
        return "?"
    m, _ = divmod(int(s), 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d:
        return f"{d}天{h}小时"
    if h:
        return f"{h}小时{m}分"
    return f"{m}分钟"


def worker_running():
    r = run(["pgrep", "-f", "mc-ha-exporter/exporter.py"])
    for pid in r.stdout.split():
        # /usr/bin/python3 实际执行体是 ...Python.app/Contents/MacOS/Python（大写 P），必须忽略大小写
        c = run(["ps", "-p", pid, "-o", "command="]).stdout.lower()
        if "python" in c and "exporter.py" in c:
            return True
    return False


def fetch_snapshot():
    cfg, _ = load_cfg()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{int(cfg.get('http_port', 8787))}/status.json"
            f"?token={cfg.get('token', '')}")
        with urllib.request.urlopen(req, timeout=4) as r:
            return cfg, json.load(r), None
    except Exception as e:
        return cfg, None, f"{e.__class__.__name__}"


def summary_text():
    lines = []
    if agent_loaded():
        lines.append("监控App：常驻中（launchd，开机自启）")
    else:
        lines.append("监控App：未常驻（未安装/已停止；双击 App 恢复）")
    if worker_running():
        lines.append("数据服务：运行中")
    else:
        lines.append("数据服务：未运行（随 App 启动拉起）")

    cfg, snap, err = fetch_snapshot()
    if snap:
        s = snap.get("server") or {}
        rc = snap.get("rcon") or {}
        mon = snap.get("monitor")
        if s.get("online"):
            lines.append(f"MC服务器：在线（已运行 {human_uptime(s.get('uptime_s'))}，"
                         f"内存 {(s.get('rss_bytes') or 0) / 1024**3:.1f}G，"
                         f"CPU {s.get('cpu_percent') if s.get('cpu_percent') is not None else '?'}%）")
        else:
            lines.append("MC服务器：离线（未开服属正常态）")
        p = rc.get("players")
        if p:
            names = "、".join(p.get("names") or []) or "无"
            lines.append(f"玩家：{p.get('online')}/{p.get('max')}：{names}")
        else:
            lines.append("玩家：无数据" if s.get("online") else "玩家：—（离线）")
        tps = rc.get("tps") or {}
        if tps.get("tps_5m") is not None or tps.get("mspt") is not None:
            t = f"TPS(5m)：{tps.get('tps_5m') if tps.get('tps_5m') is not None else '?'}"
            t += f"   Tick：{tps.get('mspt')}ms" if tps.get("mspt") is not None else ""
            lines.append(t)
        if mon:
            lines.append(f"巡检：{mon.get('verdict')}（{mon.get('checked_at', '')}）")
        lines.append("──────────────")
        lines.append(f"HA 配置 → 主机 {lan_ip()}  端口 {cfg.get('http_port', 8787)}")
        tok = cfg.get("token", "")
        lines.append(f"令牌：{tok[:6]}…（点「复制 HA 配置」取完整）")
    else:
        lines.append(f"快照不可用：{err}（数据服务未就绪或未安装）")
        lines.append("──────────────")
        lines.append(f"HA 配置 → 主机 {lan_ip()}  端口 {DEFAULT_CFG['http_port']}")
    return "\n".join(lines), cfg


# ---------------------------------------------------------------- 弹窗
def asq(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def osa(script):
    return run(["osascript", "-e", script])


def ask(text, buttons, cancel=None, default=None, timeout=None):
    b = ", ".join(asq(x) for x in buttons)
    s = f'display dialog {asq(text)} with title "MC服务器监控" buttons {{{b}}}'
    if default:
        s += f" default button {asq(default)}"
    if cancel:
        s += f" cancel button {asq(cancel)}"
    if timeout:
        s += f" giving up after {int(timeout)}"
    r = osa(s)
    if r.returncode != 0:
        if "-128" in r.stderr:
            return "__cancel__"
        return "__error__:" + (r.stderr or "").strip()[:300]
    out = r.stdout.strip()
    if "gave up:true" in out:
        return "__gaveup__"
    for x in buttons:
        # 带超时的对话框回执形如 "button returned:xxx, gave up:false"，必须用包含匹配
        if f"button returned:{x}" in out:
            return x
    return out


def alert(text, title="MC服务器监控"):
    osa(f"display alert {asq(title)} message {asq(text)}")


def copy_ha_config(cfg):
    text = (f"主机：{lan_ip()}\n"
            f"端口：{cfg.get('http_port', 8787)}\n"
            f"令牌：{cfg.get('token', '')}\n"
            f"URL：http://{lan_ip()}:{cfg.get('http_port', 8787)}/status.json")
    run(["pbcopy"], input=text)
    alert("HA 配置已复制到剪贴板，粘贴到 HA 添加集成时使用。")


def manage_dialog():
    btn = ask("选择管理操作：", buttons=["重启数据服务", "重启监控App", "打开数据目录"], timeout=90)
    if btn == "重启数据服务":
        try:
            run(["pkill", "-f", "mc-ha-exporter/exporter.py"])
            time.sleep(2)
            cfg, _, _ = fetch_snapshot()
            ok = wait_exporter(cfg or DEFAULT_CFG, 12)
            alert("数据服务已重启" + ("，接口正常。" if ok else "，但接口暂未就绪（稍后重开面板查看）。"))
        except Exception as e:
            alert(f"重启失败：{e}")
    elif btn == "重启监控App":
        agent_stop()
        time.sleep(3)
        agent_bootstrap()
        alert("监控App已重启。")
    elif btn == "打开数据目录":
        run(["open", WORK])


def dialog_loop():
    """弹窗面板模式（swiftc 不可用时的回退主程序）。"""
    while True:
        try:
            ensure_install()
        except Exception as e:
            alert(f"安装/启动失败：{e}\n\n{WORK}")
            return
        text, cfg = summary_text()
        btn = ask(text, buttons=["复制 HA 配置", "管理…", "退出"],
                  cancel="退出", default="复制 HA 配置",
                  timeout=int(os.environ.get("MCX_DIALOG_TIMEOUT", "90")))
        if btn == "复制 HA 配置":
            copy_ha_config(cfg)
        elif btn == "管理…":
            manage_dialog()
        else:  # 退出 / gave up→刷新重开
            if btn == "__cancel__" or btn == "退出":
                return


# ---------------------------------------------------------------- CLI
def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "--panel"
    if cmd == "--panel":
        dialog_loop()
    elif cmd == "--install":
        notes, warns = ensure_install(force_restart=True)
        cfg, _, _ = fetch_snapshot()
        ok = wait_exporter(cfg or DEFAULT_CFG)
        print("安装：" + "；".join(notes))
        for w in warns:
            print("警告：" + w)
        print("接口就绪" if ok else "接口未就绪（看 app.err.log / exporter.err.log）")
        print(summary_text()[0])
    elif cmd == "--ensure":
        # 幂等自愈：菜单栏 App 每次启动调用。健康时无操作、不重启任何东西。
        notes, warns = ensure_install()
        print("ensure：" + ("；".join(notes) if notes else "无变更"))
        for w in warns:
            print("警告：" + w)
    elif cmd == "--status":
        print(summary_text()[0])
    elif cmd == "--restart":
        notes, warns = ensure_install(force_restart=True)
        print("已重启" + ("：" + "；".join(notes) if notes else ""))
        for w in warns:
            print("警告：" + w)
    elif cmd == "--stop":
        agent_stop()
        print("已停止。App 退出后工作进程会随之自退（约 5 秒内）；双击 App 可恢复。")
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()

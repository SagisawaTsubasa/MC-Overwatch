# MC 服务器 → Home Assistant 自定义集成方案

> **2026-10-01 注**：项目已通用化并更名 **MC-Overwatch**（domain `mc_overwatch`，与任何特定服务器无关）。本文中的 `xuanqiong_mc` / 璇穹之歌 为历史名称，现状以代码与 README 为准。
>
> 版本 2026-09-27 ｜ 状态：设计定稿，待实现
> 本文档自包含，可独立于本机环境在任意机器（Windows/git）上开发。
> **安全约定：RCON 密码与 token 一律用占位符，真实值只存在于 Mac 本地配置，绝不进 git。**

---

## 1. 背景与现状

| 项 | 现状 |
|---|---|
| MC 服务器 | `/Users/luzeyi/Documents/MC/Server`（璇穹之歌 1.2.0.8，NeoForge 21.1.251 / MC 1.21.1，364 jar），运行在这台 Mac mini（16G）上 |
| 启动方式 | 用户双击 `启动服务器.command` 前台运行；不开服时 25565/25575 均拒绝连接（**集成必须把"离线"当正常态**） |
| MC 端口 | 25565；`online-mode=false`（离线模式） |
| RCON | **已开启**（`enable-rcon=true`，端口 **25575**，密码在 server.properties 的 `rcon.password`），仅监听本机/局域网，frp 未转发此端口 |
| frp 中转 | frps 在上海 ECS `8.133.191.231`，`allowPorts` 仅 25565， friends 入服走 `mc.sagizawa.top`；**8787 等其他端口不对公网开放** |
| 现有巡检 | `/Users/luzeyi/Documents/MC/server-monitor/check.py`，每小时 cron（automation-01b33bee-c47b-40f9-bf5f-33a5eac5ef65），产出判级 OK/WARN/FAULT + OOM/FATAL/延迟计数 + chunky 进度，写 `status.log` |
| 现有 RCON 客户端 | `/Users/luzeyi/Documents/MC/seedroll/rconclient.py`（纯标准库，已被 check.py 长期使用，可靠） |
| Home Assistant | 与本 Mac **同一局域网**，可直接访问 Mac 的内网 IP（25565 / 25575 / 8787 全部可达） |

### 关键环境约束（踩过的坑，开发时必须遵守）

1. **macOS TCC**：launchd 用户代理**无权读写 `~/Documents`**（静默拦截，表现为进程活着、日志空、零文件访问）。所有 launchd 常驻程序的二进制/配置/日志必须放 `~/Library` 等非 TCC 目录 → exporter 装在 `~/Library/mc-ha-exporter/`。
2. 由于 (1)，exporter 不能直接读 `~/Documents` 下的 server.properties 和 check.py 产物 → RCON 密码复制进 exporter 自己的 config.json；巡检结果由 check.py **主动写出来**（见 §5）。
3. 文件名一律 ASCII（Windows 侧 git + GBK 乱码前科）；内容 UTF-8。Windows 仓库建议 `git config core.quotepath false`。
4. check.py 的历史教训：RCON 对 `say`/条件命令可能无回显；解析器必须对输出格式做多格式容错并把原文放进属性。

---

## 2. 总体架构

```
┌─────────────────────── Mac（MC 服务器所在机）───────────────────────┐
│  ~/Library/mc-ha-exporter/exporter.py   ← launchd 常驻 (mc.ha-exporter)
│    ├─ pgrep/ps          → 进程存活 / RSS / CPU% / uptime
│    ├─ TCP connect 25565 → 端口在线探测
│    ├─ RCON 25575        → list（玩家）、tick query / spark tps（TPS/MSPT）
│    └─ 合并 last_check.json（check.py 透传）
│         ↓ 每次刷新重算快照，HTTP 暴露（仅局域网）
│    http://<mac-ip>:8787/status.json （鉴权：X-Export-Token 请求头）
│         ↑ HA 侧每 30s 轮询（DataUpdateCoordinator）
│  check.py（既有，每小时）──新增──→ ~/Library/mc-ha-exporter/last_check.json
└────────────────────────────────────────────────────────────────────┘
                                │
┌───────────────────── HA（同一局域网）───────────────────────────────┐
│  custom_components/mc_overwatch/
│    config_flow（UI 填 host/port/token）→ coordinator 轮询 → 8 个实体
└────────────────────────────────────────────────────────────────────┘
```

**为什么这么分层**：内存 RSS/CPU/进程信息只有 Mac 本机拿得到；RCON 协议复杂度放在 Mac 侧（复用已验证代码），HA 组件退化成"一个 HTTP 轮询 + 实体映射"，代码量小、故障面窄、两端可独立开发（契约见 §3）。

---

## 3. status.json 数据契约（两端对接的唯一接口）

Exporter 每次请求返回当前快照。**所有字段都可能为 null/缺失，HA 侧必须容错。**

```jsonc
{
  "ts": 1769500000,                    // 快照生成时间 epoch 秒
  "server": {
    "online": true,                    // = ready（真可进服才 true，binary_sensor 直接用）
    "process_alive": true,             // pgrep -x java 且命令行含 unix_args（精确判定）
    "port_open": true,                 // TCP connect 127.0.0.1:25565 成功（2s 超时）；
                                       //   25565 是 ZstdNet 代理入口，只证明代理在 accept
    "ready": true,                     // SLP 握手成功（穿透代理到 MC 后端）= 玩家可进服
    "pid": 82122,
    "uptime_s": 123456,                // ps etime 换算
    "started_at": "2026-09-27T16:11:00+08:00",  // now - etime，ISO8601 带时区
    "rss_bytes": 8262541312,           // ps rss × 1024；进程不在则 null
    "cpu_percent": 12.3                // ps pcpu；进程不在则 null
  },
  "rcon": {
    "ok": false,                       // list 命令成功解析才 true（逐条容错，见下）
    "error": "[Errno 61] Connection refused",     // 失败原因（任一命令失败都留因），成功则 null
    "players": {                       // list 失败/解析失败时 null
      "online": 2,
      "max": 20,
      "names": ["AluneMua", "Steve"]
    },
    "tps": {                           // tick query 失败或解析失败时 null
      "mspt": 2.3,                     // 毫秒/tick（原生 tick query，见解析策略）
      "tps_1m": 19.98,                 // 由单次 MSPT 换算（min(20, 1000/mspt)），三个窗口同值
      "tps_5m": 19.98,
      "tps_15m": 19.97,
      "raw": "Average time per tick: 2.3ms (Target: 50.0ms)"   // 原始回显，永远保留
    }
  },
  "monitor": {                         // 来自 last_check.json；文件不存在则 null
    "verdict": "OK",                   // OK | WARN | FAULT-*（FAULT 带后缀，如 FAULT-stuck）
    "checked_at": "2026-09-27 18:07:03",
    "events_1h": { "oom": 0, "fatal": 0, "lag": 3, "unload_wait": 0 },
    "chunky": "42.1% (101000/240100)"  // 原样字符串或 null
  },
  "exporter": { "version": "1.1.0", "interval_s": 15 }
}
```

语义要点：

- **服务器离线是正常态**：`server.online=false` + `rcon.ok=false` + `rcon.error="Connection refused"` 是标准离线快照，不是错误。
- **online = ready（SLP 就绪）**：启动加载世界期间 ZstdNet 代理已 accept 但 MC 后端未就绪 → `port_open=true, ready=false, online=false`（"启动中"在 HA 显示为离线，属正确语义）。
- **RCON 逐条容错**（exporter ≥1.1.0）：`list` 与 `tick query` 各自独立 try；`list` 成功而 tick 失败时 `players` 有值、`tps=null`、`error` 留因——单条命令失败不再丢整份快照。
- `rcon.ok=true` 但 tick 回显解析失败 → `tps` 各数值 null，`raw` 保留原文。
- exporter 本身挂了 = HA 侧 HTTP 404/超时 → coordinator 抛 `UpdateFailed`，全部实体进入 `unavailable`（区别于"服务器离线"，后者实体仍可用、值为 off/none）。
- token 认证只走 `X-Export-Token` 请求头（HA 侧不再用 query 传 token，避免随日志泄露）。

---

## 4. Mac 侧 exporter 规格

### 4.1 目录与文件

```
~/Library/mc-ha-exporter/
├── exporter.py        # 单文件，纯标准库（http.server / json / subprocess / socket / threading）
├── config.json        # chmod 600，不进任何 git
├── last_check.json    # check.py 写入（§5），exporter 只读
├── exporter.log       # stdout（可轮转：launchd 无自带轮转，量小可忽略，或启动时 >2MB 截断）
└── exporter.err.log
```

### 4.2 config.json（示例值即默认值）

```json
{
  "http_port": 8787,
  "bind": "0.0.0.0",
  "token": "<openssl rand -hex 16 生成>",
  "mc_port": 25565,
  "rcon_host": "127.0.0.1",
  "rcon_port": 25575,
  "rcon_password": "<从 server.properties rcon.password 复制>",
  "rcon_timeout_s": 10,
  "refresh_interval_s": 15
}
```

> 部署时先查端口占用：`lsof -nP -iTCP:8787 -sTCP:LISTEN`，被占则换（如 8791）并同步改 HA 配置。

### 4.3 exporter.py 行为规格

- `ThreadingHTTPServer`；请求只读缓存快照（带锁），**不在请求线程里做采集**。
- 后台线程每 `refresh_interval_s` 刷新一轮：
  1. `pgrep -f unix_args.txt` → pid；有 pid 则 `ps -o rss=,pcpu=,etime= -p <pid>`（与 check.py 同口径）。
  2. `socket.create_connection(("127.0.0.1", mc_port), 2)` → port_open。
  3. online 时才做 RCON：认证（type 3）→ `list` → `tick query` → `spark tps`（每条独立 try，单条失败不影响其他）。
  4. 合并 `last_check.json`（存在且 JSON 合法则放入 monitor，否则 null）。
- 鉴权：`?token=`（本机调试方便）或请求头 `X-Export-Token`（HA 侧标准用法），不匹配返回 `403 {"error":"forbidden"}`。
- 日志：仅记启动、刷新异常栈（低频）；正常刷新不刷屏。

**RCON 输出解析策略（关键实现点）**：

| 命令 | 已知格式 | 正则 | 备注 |
|---|---|---|---|
| `list` | `There are 2 of a max of 20 players online: AluneMua, Steve`（0 人时无冒号段） | `There are (\d+) of a max of (\d+) players online(?::\s*(.+))?` | names 按逗号+空格切；格式已验证过（seedroll 在用） |
| `spark tps` | `TPS from 1m, 5m, 15m: 19.98, 19.98, 19.97` | `TPS from 1m, 5m, 15m:\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)` | RCON 通道输出可能带 `§` 颜色码，解析前先剥掉 |
| `tick query` | 1.20.3+ 原生输出，**具体格式未实测** | 多格式容错：任何 `\d+(\.\d+)?\s*ms` 数字序列里取 avg；失败则 mspt=null | **开服后 live 校验一次正则**（§8） |

> MSPT 优先级：`tick query` 解析成功 > spark tps 换算（TPS>0 时 mspt≈1000/TPS）> null。

**RCON 线协议附录（自包含，Windows 开发可直接用）**：TCP；小端；`[length:int32][request_id:int32][type:int32][payload][0x00 0x00]`，length 不含自身 4 字节。type：3=登录（payload=密码，回包 id=-1 即失败）、2=执行命令、0=响应。参考实现：`/Users/luzeyi/Documents/MC/seedroll/rconclient.py`（部署时复制其核心进 exporter.py，保持 exporter 自包含）。

### 4.4 launchd plist（2026-09-28 整合后：Program 是 **App 本体**，不再是 python）

manager.py 自动生成，无需手写。要点：**本体必须部署在 ~/Library**（launchd 用户
代理无权读写 ~/Documents——TCC 静默拦截，从 Documents 跑常驻二进制必死）；
KeepAlive 用 `{SuccessfulExit: false}` 语义 = 崩溃/被杀自愈拉起、用户正常退出不拉起。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>mc.ha-exporter</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/luzeyi/Library/mc-ha-exporter/mc-ha-exporter.app/Contents/MacOS/mc-ha-exporter</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
    <key>StandardOutPath</key><string>/Users/luzeyi/Library/mc-ha-exporter/app.log</string>
    <key>StandardErrorPath</key><string>/Users/luzeyi/Library/mc-ha-exporter/app.err.log</string>
</dict>
</plist>
```

分层职责（整合后）：**App 本体 = 唯一常驻实体**（菜单栏图标即存活确认）；python
采集工作进程是 App 的子进程（App 内置监管器：死亡 8s 节流重生、连续 3 次 HTTP
无响应判卡死杀掉重生、启动时 pkill 清孤儿；worker 侧有父进程看门狗，getppid==1
即自退——双保险确保数据服务随 App 存亡）。worker 的 stdout/stderr 由监管器接回
`exporter.log`/`exporter.err.log`，排障契约不变。

安装 / 验证：

```bash
bash macapp/build-app.sh                          # 构建 → 复制到 ~/Documents/MC/
open ~/Documents/MC/mc-ha-exporter.app            # 双击 = 安装：部署本体+激活 launchd
launchctl print gui/$(id -u)/mc.ha-exporter | grep -E "state|program"   # 期望 running + App 路径
pgrep -f "mc-ha-exporter"                         # 应看到 App 本体 + python 工作进程（父子）
curl -s "http://127.0.0.1:8787/status.json?token=<TOKEN>" | python3 -m json.tool
curl -s -o /dev/null -w '%{http_code}\n' "http://127.0.0.1:8787/status.json"   # 期望 403
kill -9 <App pid> && sleep 12 && launchctl print gui/$(id -u)/mc.ha-exporter | grep pid   # 崩溃自愈：新 pid、新 worker
```

> macOS 应用防火墙若弹出"是否允许 python3/监控 App 接受传入连接"，点允许（仅"专用网络"即可）。

### 4.5 check.py 透传补丁（规格，约 10 行）

位置：check.py 算出判级并 `append_status` 之后。幂等、异常静默（exporter 未安装时不报错）：

```python
def export_to_ha(verdict, ev, chunky_raw):
    """把本轮巡检结论写给 HA exporter（~/Library 避 TCC）。失败静默。"""
    try:
        out = {"verdict": verdict, "checked_at": NOW,
               "events_1h": {k: ev.get(k, 0) for k in ("oom", "fatal", "lag", "unload_wait")},
               "chunky": chunky_raw}
        path = "/Users/luzeyi/Library/mc-ha-exporter/last_check.json"
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(out, f)
        os.replace(tmp, path)          # 原子替换，exporter 永远读不到半个文件
    except Exception:
        pass
```

`chunky_raw` 取本轮 RCON `chunky progress` 的原始回显（`rcon_readonly()` 已有）；不改动 check.py 任何现有逻辑。

---

## 5. HA 侧 custom component 规格

域：`mc_overwatch`；设备名：`MC 服务器`（一个 device，8 实体挂其下；`has_entity_name=True`，unique_id 用 `{entry.entry_id}-<key>` 保证改名不乱）。

### 5.1 文件树

```
custom_components/mc_overwatch/
├── manifest.json
├── const.py            # DOMAIN, CONF_TOKEN, DEFAULT_PORT=8787, DEFAULT_SCAN=30, 各实体 key
├── __init__.py         # 建 aiohttp_client session + Coordinator；async_unload_entry 转发卸载
├── config_flow.py      # host / port / token / scan_interval；提交时实测 GET /status.json 校验（403/超时给出对应错误码）
├── coordinator.py      # McServerCoordinator(DataUpdateCoordinator[dict])：GET http://host:port/status.json（X-Export-Token 头），timeout 10s；403 → ConfigEntryAuthFailed（触发 reauth）；连接/超时/非JSON → UpdateFailed
├── entity.py           # McServerEntity 基类：device_info / _attr_has_entity_name / availability
├── binary_sensor.py    # 1 个
├── sensor.py           # 7 个
├── strings.json        # 与 translations/en.json 相同内容（HA 惯例）
└── translations/
    ├── en.json
    └── zh-Hans.json    # UI 全中文
```

`manifest.json`：

```json
{
  "domain": "mc_overwatch",
  "name": "MC 服务器",
  "codeowners": ["@luzeyi"],
  "config_flow": true,
  "iot_class": "local_polling",
  "requirements": [],
  "version": "0.1.0"
}
```

### 5.2 实体定义（8 个）

| 实体 key | 平台 | 中文名 | 数据来源 | 属性 | class/unit/state_class |
|---|---|---|---|---|---|
| online | binary_sensor | 服务器在线 | `server.online`（=ready，真可进服） | pid, port_open, uptime_s, rcon_error | device_class=connectivity |
| players | sensor | 在线玩家 | `rcon.players` | 玩家名单(names)、max | unit=人, icon=mdi:account-multiple, state_class=measurement |
| tps | sensor | TPS(5分钟) | `rcon.tps.tps_5m` | tps_1m/tps_15m/mspt/raw | unit=tps, icon=mdi:speedometer, state_class=measurement |
| mspt | sensor | Tick耗时 | `rcon.tps.mspt` | raw | unit=ms, icon=mdi:timer-outline, state_class=measurement |
| memory | sensor | JVM内存 | `server.rss_bytes` | — | device_class=data_size, unit=GB(2位小数), state_class=measurement |
| cpu | sensor | CPU占用 | `server.cpu_percent` | — | unit=%, icon=mdi:cpu-64-bit, state_class=measurement |
| uptime | sensor | 运行时长 | `server.started_at` | uptime_s | device_class=timestamp（HA 自行显示"x 小时前"） |
| verdict | sensor | 巡检判级 | `monitor.verdict` | events_1h, chunky, checked_at | icon=mdi:heartbeat-pulse（state: OK/WARN/FAULT-*） |

**availability 语义**（entity.py 统一实现）：

- coordinator 最近一次更新失败 → 全部 `unavailable`。
- `server.online=false`：memory/cpu/uptime/players/tps/mspt → `unavailable`（或按需求显示"最后已知值+标记离线"，默认取 unavailable 更直观）；**online binary_sensor 本身永远 available**（值=off）。
- `rcon.ok=false` 但进程活着（罕见）：仅 players/tps/mspt unavailable。
- `monitor=null`（exporter 无 last_check.json）：仅 verdict unavailable。

### 5.3 翻译键（zh-Hans）

`config.step.user`：host（主机地址）、port（端口）、token（访问令牌）、scan_interval（刷新间隔，秒）；error：`cannot_connect`（连接失败）、`invalid_auth`（token 错误）、`unknown`。实体名按上表"中文名"列。

---

## 6. 安全要点

1. **RCON 密码**：只存在于 Mac 本地 `~/Library/mc-ha-exporter/config.json`（chmod 600）。文档/仓库一律占位符。
2. **token**：`openssl rand -hex 16`；同一份值填进 exporter config 和 HA 集成配置。
3. **网络**：exporter 只绑定局域网（0.0.0.0 但**严禁**把 8787 加进 frp allowPorts 或云安全组）；RCON 25575 永不暴露公网。公网仍只有 25565。
4. **git 卫生**：仓库 `.gitignore` 至少含：`config.json`、`last_check.json`、`*.log`、`__pycache__/`；提供 `config.example.json` 与 `mc.ha-exporter.plist.example`（含 `<YOUR_USERNAME>` 占位）。

---

## 7. 仓库建议布局（Windows 侧 git）

```
mc-ha-exporter/
├── docs/mc-ha-integration-plan.md        # 本文档
├── exporter/exporter.py                  # Mac 侧，开发/单测可在 Windows（纯标准库）
├── exporter/config.example.json
├── ha-component/custom_components/mc_overwatch/   # 整个目录拷进 HA 的 /config/custom_components/
├── mac-install/mc.ha-exporter.plist.example
├── mac-install/patch-checkpy.md          # §4.5 的落地说明
└── .gitignore
```

HA 组件开发自测（无 HA 实例时）：Python ≥3.12 语法级检查 + 用一个 mock HTTP 服务冒烟跑 coordinator 逻辑；最终以 §8 验收为准。HA 兼容目标：2024.8+。

---

## 8. 测试与验收清单

**Mac 侧（服务器关闭态即可测）**
- [ ] `curl` 带 token 返回合法 JSON：`server.online=false`、`rcon.ok=false`、`monitor=null`（未打 check.py 补丁前）
- [ ] 无 token → 403；错误 token → 403
- [ ] launchd：state=running；`kill -9` 后 3 秒内自愈
- [ ] exporter 日志无 TCC `PermissionError`（一切读路径都在 ~/Library）
- [ ] 打 check.py 补丁后等整点巡检：`last_check.json` 出现且 verdict/events/chunky 齐全（或手动 `python3 check.py` 触发一次）

**开服后 live 校验（第一次开服时做）**
- [ ] `tick query` / `spark tps` 真实回显 vs 正则：players/tps/mspt 数值正确，raw 属性有原文；解析不动则调正则
- [ ] 玩家进/出服：players 数值与名单实时变化（≤45s 内）
- [ ] RSS/CPU 与 `ps` 手工值一致；停服 → online 翻 off

**HA 侧**
- [ ] 拷入 custom_components → 重启 → 添加集成"MC 服务器"（UI 中文）→ 8 实体出现
- [ ] 配置错误 token → 表单报"token 错误"而非崩溃；exporter 停掉 → 实体 unavailable，恢复后自愈
- [ ] 自动化示例可用：
  ```yaml
  # 服务器离线告警
  - trigger: { platform: state, entity_id: binary_sensor.服务器在线, to: "off", for: "00:02:00" }
    action: { service: notify.persistent_notification, data: { message: "MC 服务器离线" } }
  # 玩家上线通知：trigger sensor.mc_overwatch_players state 从 0 → >0
  # TPS 告警：numeric_state below 18 for 00:05:00
  # 巡检告警：verdict state → FAULT
  ```

---

## 9. 部署 Checklist（总装）

**Mac 侧（一次）**
1. 建 `~/Library/mc-ha-exporter/`，放入 exporter.py、config.json（填真实密码+token，chmod 600）
2. `launchctl bootstrap` + §4.4 验证四连
3. check.py 加 §4.5 补丁，手动跑一轮确认 last_check.json
4. `ipconfig getifaddr en0` 记下 Mac 内网 IP；**路由器给 Mac 做 DHCP 保留**（IP 变了 HA 会断）
5. macOS 防火墙放行 python3 入站（专用网络）

**HA 侧（一次）**
6. `custom_components/mc_overwatch/` 拷到 HA `/config/custom_components/`（Samba / SFTP / Studio Code Server 任一）
7. 重启 HA → 设置→设备与服务→添加集成→"MC 服务器"，填 Mac IP / 8787 / token / 30s
8. 对照 §8 验收

---

## 10. 明确不做 & 后续可选扩展

**本版不做**：控制能力（从 HA 重启/发公告——用户未选择；RCON 采集仅只读）；不动 Server 目录任何配置。

**后续可选**（均向后兼容，不破坏 §3 契约）：
- **控制按钮**：HA `button`/`select` 平台 → exporter 增 POST 端点（白名单命令：`say`、受控重启流程复用 check.py 的 `--restart` 决策树），必须加二次确认与玩家在线保护
- **chunky 进度独立 sensor**：monitor.chunky 解析成数值
- **MQTT 备选路线**：若日后 HA 迁移/跨网段，exporter 可加 MQTT 发布（同一快照结构），HA 换 MQTT 集成，本方案 HA 组件作废成本极低
- **多服务器**：config_flow 已按单机设计，扩展即多 config entry，天然支持

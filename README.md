# MC Overwatch — Home Assistant 集成 + Mac 状态导出器

把 Mac 上运行的 Minecraft 服务器（NeoForge 1.21.1 实测）的运行信息同步到 Home Assistant：

- 服务器在线 / 离线（binary_sensor，离线是正常态不是错误）
- 在线玩家数 + 名单
- TPS 与每 tick 耗时（MSPT，原生 tick query）
- JVM 内存（RSS）、CPU 占用、运行时长
- 每小时巡检判级（OK / WARN / FAULT-*，来自 Mac 上既有的 check.py 巡检）

> 命名典故：Overwatch 取"守望"意，项目本身与任何特定服务器无关——任何 Mac 上的
> MC 服务器装上导出器即可接入（作者自己的服叫"璇穹之歌"，仅作首测环境）。

架构两层：Mac 侧常驻一个**导出器**（exporter，聚合本机数据 + RCON 查询，以带 token 的
HTTP JSON 对局域网暴露）；HA 侧是**自定义集成**（轮询该 JSON 并实体化）。
两端通过 `status.json` 一个契约对接，可独立开发。

```
┌────────────────────────── Mac ───────────────────────────┐
│ MC服务器监控.app（唯一常驻实体，菜单栏可见 ⛏ 20.0T 2人 7.3G）│
│   launchd 代理 mc.ha-exporter 拉起 App 本体               │
│   （本体在 ~/Library，开机自启 + 崩溃自愈 + 正常退出不拉起） │
│    └─ 内部子进程：python 采集工作进程（随 App 存亡，       │
│       有父进程看门狗；ps/SLP探活/RCON(list/tick query)）   │
│    http://<mac-ip>:8787/status.json （鉴权 X-Export-Token 头）│
└──────────────────────────────────────────────────────────┘
            ↑ 每 30s 轮询（aiohttp coordinator）
┌──────────────── HA（同一局域网）────────────┐
│ custom_components/mc_overwatch → 8 个实体    │
└─────────────────────────────────────────────┘
```

> 双份进程已成为历史（2026-09-28 整合）：独立的 launchd python 服务已并入 App。
> 菜单栏图标就是数据服务活着的确认；退出 App = 数据服务一并停止（约 5 秒内）。

## 仓库结构

```
├── hacs.json                        # HACS 元数据
├── custom_components/mc_overwatch/  # HA 自定义集成（HACS 标准位置）
├── macapp/                          # Mac App 源码（独立子文件夹）
│   ├── exporter/exporter.py         # 导出器本体（纯标准库）
│   ├── app/manager.py               # App 双击入口：安装/launchd 托管/状态面板
│   ├── app/statusbar.swift          # 菜单栏主程序（AppKit，swiftc 编译）
│   ├── app/Info.plist               # App 包描述（LSUIElement，不占 Dock）
│   └── build-app.sh                 # ★一键构建脚本（编译逻辑见下文专节）★
├── tests/                           # 离线测试（stub HA，无 HA 环境可跑）
├── mc-ha-integration-plan.md        # 设计文档（含 status.json 完整契约 §3）
├── pyproject.toml                   # ruff 配置
├── requirements-dev.txt             # 开发依赖（ruff）
├── .github/workflows/ci.yml         # CI：ruff + 三份离线测试
├── LICENSE                          # MIT
└── .gitignore                       # 排除 config.json / last_check.json / 日志
```

## 安装

### 第 1 步：Mac 侧（装导出器）

在本机仓库目录执行（或直接用构建好的 app）：

```bash
bash macapp/build-app.sh
```

然后双击 `~/Documents/MC/mc-ha-exporter.app`。首次运行：

1. 弹出"文稿文件夹访问"授权 → **允许**（要读 server.properties 里的 RCON 密码，只需一次）；
2. 弹回执"已部署/已启动"后，监控本体常驻在**菜单栏**（⛏ 图标）——Documents 里的
   副本只是安装器，本体已部署到 `~/Library/mc-ha-exporter/mc-ha-exporter.app`
   （launchd 无权读 Documents，常驻二进制必须在 ~/Library）；
3. 点菜单栏图标 → **复制 HA 配置**，剪贴板里是主机/端口/令牌三件套。

内部做的事：把采集工作进程 exporter.py 装到 `~/Library/mc-ha-exporter/`
（config.json 存 RCON 密码与 token，0600），把 App 本体也部署到 ~/Library，
注册 launchd 代理 `mc.ha-exporter` 直接拉起**本体 App**（RunAtLoad +
KeepAlive{SuccessfulExit:false}：开机自启、崩溃自愈、用户正常退出不拉起）。
本体 App 内部再以子进程方式运行工作进程（有父进程看门狗，随 App 存亡）。
**再次双击 Documents 副本 = 检查更新/重新部署**；日常看状态、管理都在菜单栏。

> 端口默认 8787（仅局域网）。若被占用改 `~/Library/mc-ha-exporter/config.json` 的
> `http_port` 后用菜单"重启数据服务"。**绝不要把 8787 加进 frp 等公网转发。**

### 第 2 步：HA 侧（装集成）

**方式 A — HACS（推荐）**：把这个仓库推到 GitHub 后，
HACS → 右上角 → 自定义存储库 → 填仓库地址、类别选「集成 (Integration)」→ 安装。

**方式 B — 手动**：把 `custom_components/mc_overwatch/` 整个目录拷到
HA 的 `/config/custom_components/` 下（Samba / SFTP / Studio Code Server 任一途径）。

然后重启 HA → 设置 → 设备与服务 → 添加集成 → 搜索「MC 服务器」→
粘贴 App 复制的三件套（主机 = Mac 内网 IP，端口 8787，令牌）→ 完成。
全中文界面，共 8 个实体挂在一个设备下。

> 建议在路由器给 Mac 做 **DHCP 保留**（固定内网 IP），IP 变了 HA 会连不上。
> macOS 防火墙首次可能弹"是否允许 python3 接受传入连接"→ 点允许（专用网络即可）。

## 实体一览

| 实体 | 说明 | 离线时 |
|---|---|---|
| binary_sensor.服务器在线 | 进程存活且 SLP 握手就绪（真可进服，ZstdNet 代理起来了但 MC 后端没就绪时不算在线）；属性带 pid / 端口 / RCON 错误 | 导出器正常时可用，值=off |
| sensor.在线玩家 | 属性：玩家名单、最大人数 | unavailable |
| sensor.TPS（5分钟） | 由 tick query 的 MSPT 换算（封顶 20；spark 经 RCON 无回显）；属性：1m/15m、MSPT、原始回显 | unavailable |
| sensor.Tick 耗时 | MSPT（原生 tick query 实测格式）；属性：原始回显 | unavailable |
| sensor.JVM 内存 | 进程 RSS（GB） | unavailable |
| sensor.CPU 占用 | ps pcpu 口径 | unavailable |
| sensor.运行时长 | device_class=timestamp，显示"3 小时前"并自行走秒；属性：uptime_s | unavailable |
| sensor.巡检判级 | OK / WARN / FAULT-stuck / FAULT-dead-* 原文；属性：事件计数、chunky 进度、巡检时间 | monitor 缺失时 unavailable |

导出器本身挂掉 → 全部实体 unavailable（与"服务器离线但导出器正常"区分开）。

改配置不用删除重建：HA 集成条目菜单里「重新配置」改主机/端口/令牌（实体与历史保留），
「配置选项」里调刷新间隔；令牌失效（403）会自动弹出重新认证。

## 自动化示例

```yaml
# 服务器离线告警（持续 2 分钟才报，避免重启误报）
automation:
  - alias: MC 服务器离线告警
    trigger:
      - platform: state
        entity_id: binary_sensor.mc_overwatch_server_online  # 实体 ID 以实际为准
        to: "off"
        for: "00:02:00"
    action:
      - service: notify.persistent_notification
        data: { message: "MC 服务器离线" }

# 玩家上线通知：trigger 用 numeric_state sensor.<玩家> above 0
# 卡顿告警：numeric_state sensor.<TPS> below 18 for 00:05:00
# 巡检故障告警：state sensor.<判级> to "FAULT-stuck"（FAULT 开头可用模板匹配）
```

## 数值实体写入语义（重要）

自 0.2.2 起，5 个数值传感器（在线玩家 / TPS / MSPT / 内存 / CPU）开启了
`force_update`：**每次轮询（默认 30 秒）都会写一条 recorder 历史点，值不变也写**，
以保证折线图历史稠密（慢变实体不再被画成跨窗口斜线）。由此有两个副作用，写自动化时务必避开：

1. **这 5 个实体的 `last_changed` 每次轮询都会刷新**。
   `condition: state ... for:`（state 条件 + for）在这 5 个实体上永远不成立；
   用 `last_changed` / `relative_time` 计算"保持某值多久"的模板也会失真。
2. **不要用不带 `from`/`to` 的裸 `state` 触发器**指向这 5 个实体——值不变也会触发，
   自动化会每 30 秒执行一次（配了 `for:` 时到点后还会周期重复）。

正确写法：state 触发器带 `to:` / `from:`（如上方离线告警示例），或使用 `numeric_state`
触发器（如上方玩家/卡顿示例）。二进制传感器（服务器在线）、运行时长、巡检判级三个
实体**未开启** force_update，其 `last_changed` 语义不变，可正常用 `for:` 判断持续时长。


## ★ Mac App 编译逻辑（macapp/）★

无需 Xcode，无需第三方依赖，一个脚本构建标准 .app 包：

```bash
cd MC-Overwatch            # 仓库根目录
bash macapp/build-app.sh
```

脚本做四件事：

1. 在 `macapp/dist/` 组装包结构（macOS 规定布局）：
   ```
   mc-ha-exporter.app/Contents/
   ├── Info.plist                     # 来自 macapp/app/Info.plist
   ├── MacOS/mc-ha-exporter           # 构建脚本生成的 bash 引导（chmod +x）
   │     exec /usr/bin/python3 ".../Resources/manager.py" "$@"
   └── Resources/
         ├── exporter.py              # 来自 macapp/exporter/exporter.py
         └── manager.py               # 来自 macapp/app/manager.py
   ```
2. `Info.plist` 关键项：`LSUIElement=true`（面板 App，不占 Dock）、
   `CFBundleIdentifier=com.luzeyi.mc-ha-exporter`；
3. 产出 `macapp/dist/mc-ha-exporter.app`，并复制一份到 `~/Documents/MC/` 供双击；
4. 不做代码签名——本机构建出的 app 不带 quarantine 属性，Gatekeeper 直接放行
   （**从别的机器下载才会被拦**：右键 → 打开，或 `xattr -dr com.apple.quarantine <app>`）。

App 运行时（manager.py）负责真正的"安装"：`~/Library/mc-ha-exporter/` 放
exporter.py + config.json（token 自动 `secrets.token_hex(16)`，RCON 密码从
server.properties 读取并每次打开 App 时自动同步），写
`~/Library/LaunchAgents/mc.ha-exporter.plist` 并 `launchctl bootstrap/kickstart`。
文件没变化且导出器在跑时，重复打开 App **不会**重启导出器。

### manager.py CLI（测试与脚本用）

```bash
APP=~/Documents/MC/mc-ha-exporter.app/Contents/Resources/manager.py
python3 $APP --install   # 安装/更新并（重）启动，打印状态
python3 $APP --ensure    # 幂等自愈（菜单栏 App 每次启动自动调用；健康时无操作）
python3 $APP --status    # 打印与面板相同的状态文本
python3 $APP --restart   # 强制重启导出器
python3 $APP --stop      # 停止（bootout；App 退出后工作进程约 5 秒内随退，双击 App 可恢复）
```

### 常见问题

| 症状 | 处置 |
|---|---|
| 面板显示"读不了 server.properties" | 系统设置 → 隐私与安全性 → 文件和文件夹 → 给本 App 勾选"文稿" |
| 菜单栏显示"⛏ 令牌错误" | 导出器侧令牌与本 App 不一致（403）：菜单「重启导出器」重新同步配置 |
| HA 提示 cannot_connect | 确认面板里 IP 与 HA 填的一致；macOS 防火墙放行 python3 入站；同一网段 |
| HA 提示 invalid_auth | 令牌不符，重新点 App 的"复制 HA 配置" |
| 实体全部 unavailable | 导出器没在跑：双击 App 或 `--install`；看 `~/Library/mc-ha-exporter/exporter.err.log` |
| 端口被占 | exporter 启动会明确报错并提示 `lsof -nP -i :8787` 排查；改 config.json 的 `http_port` → 面板"重启导出器" → HA 侧同步改端口 |
| 改了 RCON 密码 | 双击 App 即自动同步（config.json 随 server.properties 更新） |

## 安全

- RCON 密码与 token 只存 Mac 本地 `~/Library/mc-ha-exporter/config.json`（0600），**永不进 git**；
- 8787 只暴露给局域网；RCON 25575 永不暴露公网；公网仍只有 25565（frp）；
- 导出器对 MC 服务器**只读**（list / spark / tick query），无任何控制命令。

## 开发对接

两端以 `status.json` 为唯一契约，字段级 schema 见
[mc-ha-integration-plan.md](mc-ha-integration-plan.md) §3。所有字段可为 null，
消费方必须容错；`ts` 为快照时间戳，可用于判断导出器停更。

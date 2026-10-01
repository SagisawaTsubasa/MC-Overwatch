// MC服务器监控 — macOS 菜单栏常驻（LSUIElement，无 Dock 图标）
// 唯一常驻实体：launchd 代理 mc.ha-exporter 直接拉起本 App（~/Library 本体，
// 开机自启 + 崩溃自愈 + 正常退出不拉起）。数据采集（python 工作进程）由本 App
// 作为子进程监管：无独立生命周期（worker 侧有父进程看门狗），随 App 存亡；
// 菜单栏图标就是它活着的确认。由 build-app.sh 以 swiftc 编译。纯 AppKit。

import AppKit
import Darwin

let workDir = NSHomeDirectory() + "/Library/mc-ha-exporter"
let lockPath = workDir + "/app.lock"

/// canonical = ~/Library 里的常驻本体（launchd 拉起，承担全部职责）；
/// installer = ~/Documents 里的安装副本（双击 = 部署+激活常驻，回执后退出）。
let runMode: RunMode = Bundle.main.bundleURL.path.hasPrefix(workDir) ? .canonical : .installer
enum RunMode { case canonical, installer }

final class AppDelegate: NSObject, NSApplicationDelegate {
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    var token = ""
    var port = 8787
    var lanIP = "…"
    var lastSummary = "正在获取状态…"
    var timer: Timer?
    let supervisor = WorkerSupervisor()
    var consecutivePollFail = 0

    func applicationDidFinishLaunching(_ note: Notification) {
        _ = NSApp.setActivationPolicy(.accessory)
        if runMode == .installer {
            installAndExit()
            return
        }
        guard acquireLock() else { exit(0) }   // 已有常驻实例在跑（图标就在菜单栏）
        refreshLanIP()
        buildMenu()
        item.button?.title = "⛏ …"
        supervisor.killOrphans()
        selfHeal()
        poll()
        timer = Timer.scheduledTimer(withTimeInterval: 15, repeats: true) { [weak self] _ in
            self?.poll()
        }
    }

    /// 双击安装副本：确保一切就绪（本体/工作进程/launchd 代理），回执后退出。
    func installAndExit() {
        runManagerCli("--ensure") { result in
            let a = NSAlert()
            a.messageText = "MC服务器监控"
            a.informativeText = result.displayText
            a.addButton(withTitle: "好")
            a.runModal()
            NSApp.terminate(nil)
        }
    }

    /// 幂等自愈：没装就装、文件有更新就同步、健康则无操作。静默，结果由 poll 反映。
    func selfHeal() {
        runManagerCli("--ensure") { [weak self] result in
            if result.ok {
                self?.supervisor.ensure()
            } else {
                self?.setUI(title: "⛏ 自愈失败",
                            summary: "manager --ensure 失败：\n\(result.displayText)")
            }
            self?.poll()
        }
    }

    /// 调用同包 manager.py CLI；校验退出码并回传 stdout+stderr（不假成功）。
    func runManagerCli(_ arg: String, done: @escaping (CliResult) -> Void) {
        DispatchQueue.global(qos: .userInitiated).async {
            let p = Process()
            p.launchPath = "/usr/bin/python3"
            let res = Bundle.main.resourceURL?.path ?? ""
            p.arguments = [res + "/manager.py", arg]
            let outPipe = Pipe(), errPipe = Pipe()
            p.standardOutput = outPipe
            p.standardError = errPipe
            do { try p.run() } catch {
                DispatchQueue.main.async { done(CliResult(ok: false,
                    text: "无法启动 manager.py：\(error)")) }
                return
            }
            p.waitUntilExit()
            let out = String(data: outPipe.fileHandleForReading.readDataToEndOfFile(),
                             encoding: .utf8) ?? ""
            let err = String(data: errPipe.fileHandleForReading.readDataToEndOfFile(),
                             encoding: .utf8) ?? ""
            let ok = p.terminationStatus == 0
            var text = out.trimmingCharacters(in: .whitespacesAndNewlines)
            if !ok {
                text = "退出码 \(p.terminationStatus)\n" + text
                if !err.isEmpty { text += "\n" + err }
            }
            DispatchQueue.main.async { done(CliResult(ok: ok, text: text)) }
        }
    }

    func loadConfig() {
        guard let data = FileManager.default.contents(
            atPath: workDir + "/config.json"),
            let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { return }
        token = obj["token"] as? String ?? ""
        port = obj["http_port"] as? Int ?? 8787
    }

    /// 取局域网 IP：遍历常见接口（en0 未必是以太网口），换网后也能拿到新地址。
    func refreshLanIP() {
        for iface in ["en0", "en1", "en2", "en5", "en6"] {
            let p = Process()
            p.launchPath = "/usr/sbin/ipconfig"
            p.arguments = ["getifaddr", iface]
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = Pipe()
            guard (try? p.run()) != nil else { continue }
            p.waitUntilExit()
            let s = String(data: pipe.fileHandleForReading.readDataToEndOfFile(),
                           encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if !s.isEmpty {
                lanIP = s
                return
            }
        }
    }

    // ------------------------------------------------------------ 状态轮询
    func poll() {
        supervisor.ensure()   // 工作进程死了会按节流自动重生
        loadConfig()          // 每轮重读：改 config.json 的端口/令牌后无需重启本 App
        refreshLanIP()
        guard !token.isEmpty else {
            setUI(title: "⛏ 未配置", summary: "找不到 \(workDir)/config.json\n请双击 ~/Documents/MC/mc-ha-exporter.app 完成安装")
            return
        }
        // 回调在后台队列执行：把 port/ip/token 拷进局部值，避免跨线程读写
        let portSnapshot = port
        let ipSnapshot = lanIP
        let tokenSnapshot = token
        var comps = URLComponents()
        comps.scheme = "http"
        comps.host = "127.0.0.1"
        comps.port = portSnapshot
        comps.path = "/status.json"
        comps.queryItems = [URLQueryItem(name: "token", value: tokenSnapshot)]
        guard let url = comps.url else {
            setUI(title: "⛏ 配置错误", summary: "config.json 的 token/端口无法构造合法 URL，请检查后用菜单「重启数据服务」")
            return
        }
        var req = URLRequest(url: url)
        req.timeoutInterval = 4
        URLSession.shared.dataTask(with: req) { [weak self] data, response, _ in
            guard let self = self else { return }
            let code = (response as? HTTPURLResponse)?.statusCode ?? 0
            if code == 403 {
                self.setUI(title: "⛏ 令牌错误",
                           summary: "数据服务拒绝访问（403）：令牌与配置不一致。\n可用菜单「重启数据服务」后重试。")
                return
            }
            guard code == 200, let data = data,
                  let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
                // spawn 后 6s 内是宽限期（worker 可能还在启动），不计数
                if Date().timeIntervalSince(self.supervisor.lastSpawnDate) > 6 {
                    self.consecutivePollFail += 1
                    let n = self.consecutivePollFail
                    if n >= 3 {
                        self.consecutivePollFail = 0
                        self.supervisor.restart()   // 工作进程疑似卡死，杀掉重生
                    }
                    self.setUI(title: "⛏ 数据服务?",
                               summary: "数据服务无响应（HTTP \(code)），已连续 \(n) 次\n看 \(workDir)/exporter.err.log")
                } else {
                    self.setUI(title: "⛏ …", summary: "数据服务启动中…")
                }
                return
            }
            self.consecutivePollFail = 0
            let r = AppDelegate.render(obj, port: portSnapshot, ip: ipSnapshot)
            self.setUI(title: r.0, summary: r.1)
        }.resume()
    }

    func setUI(title: String, summary: String) {
        DispatchQueue.main.async {
            self.item.button?.title = title
            self.lastSummary = summary
        }
    }

    // 快照 → (菜单栏标题, 详情文本)。字段契约见仓库 README。
    static func render(_ o: [String: Any], port: Int, ip: String) -> (String, String) {
        let srv = o["server"] as? [String: Any] ?? [:]
        let rcon = o["rcon"] as? [String: Any] ?? [:]
        let mon = o["monitor"] as? [String: Any]
        let tps = rcon["tps"] as? [String: Any] ?? [:]
        let players = rcon["players"] as? [String: Any]
        let online = srv["online"] as? Bool ?? false

        var lines: [String] = []
        var title = "⛏ 离线"
        if online {
            let n = (players?["online"] as? Int) ?? 0
            let maxP = (players?["max"] as? Int) ?? 20
            let t5 = tps["tps_5m"] as? Double
            let mspt = tps["mspt"] as? Double
            let mem = (srv["rss_bytes"] as? Double ?? 0) / 1073741824.0
            let cpu = srv["cpu_percent"] as? Double
            let memCpu = String(format: "内存：%.1fG   CPU：%@", mem,
                                cpu.map { String(format: "%.0f%%", $0) } ?? "?")
            // 标题加内存占用（进程 RSS，与 HA 内存实体/巡检预警线同口径）
            let memPart = mem > 0 ? String(format: " %.1fG", mem) : ""
            title = "⛏ \(t5.map { String(format: "%.1fT", $0) } ?? "?T") \(n)人\(memPart)"
            lines.append("服务器：在线")
            lines.append("玩家：\(n)/\(maxP)")
            lines.append("TPS：\(t5.map { String(format: "%.2f", $0) } ?? "?")   Tick：\(mspt.map { String(format: "%.1fms", $0) } ?? "?")")
            lines.append(memCpu)
        } else {
            lines.append("服务器：离线（未开服属正常态）")
        }
        if let mon = mon, let v = mon["verdict"] as? String {
            let at = mon["checked_at"] as? String ?? ""
            lines.append("巡检：\(v)（\(at)）")
        }
        lines.append("──────────")
        lines.append("HA 配置：\(ip):\(port)（菜单里可复制）")
        return (title, lines.joined(separator: "\n"))
    }

    // ------------------------------------------------------------ 单实例锁
    /// 锁文件记 pid；除存活校验外还验进程身份（防 pid 复用误判成"已在运行"
    /// 然后自己 exit(0) —— launchd SuccessfulExit:false 不会重拉，监控静默失效）。
    func acquireLock() -> Bool {
        if let data = FileManager.default.contents(atPath: lockPath),
           let s = String(data: data, encoding: .utf8),
           let pid = Int(s.trimmingCharacters(in: .whitespacesAndNewlines)),
           pid != Int(getpid()), kill(pid_t(pid), 0) == 0 {
            let p = Process()
            p.launchPath = "/bin/ps"
            p.arguments = ["-p", String(pid), "-o", "comm="]
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = Pipe()
            guard (try? p.run()) != nil else { return false }
            p.waitUntilExit()
            let comm = String(data: pipe.fileHandleForReading.readDataToEndOfFile(),
                              encoding: .utf8) ?? ""
            if comm.contains("mc-ha-exporter") {
                return false   // 确实是自家另一个实例
            }
            // pid 已被无关进程复用：锁是陈旧的，接管
        }
        try? FileManager.default.removeItem(atPath: lockPath)
        try? String(getpid()).write(toFile: lockPath, atomically: true, encoding: .utf8)
        return true
    }

    // ------------------------------------------------------------ 菜单
    func buildMenu() {
        let m = NSMenu()
        let entries: [(String, Selector)] = [
            ("状态详情", #selector(showDetail)),
            ("复制 HA 配置", #selector(copyHA)),
        ]
        for (title, sel) in entries {
            let mi = m.addItem(withTitle: title, action: sel, keyEquivalent: "")
            mi.target = self
        }
        m.addItem(.separator())
        let mgmt: [(String, Selector)] = [
            ("重启数据服务", #selector(restartWorker)),
            ("打开数据目录", #selector(openDir)),
        ]
        for (title, sel) in mgmt {
            let mi = m.addItem(withTitle: title, action: sel, keyEquivalent: "")
            mi.target = self
        }
        m.addItem(.separator())
        m.addItem(withTitle: "退出（同时停止数据服务）",
                  action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        item.menu = m
    }

    @objc func showDetail() {
        poll()
        let a = NSAlert()
        a.messageText = "MC服务器监控"
        a.informativeText = lastSummary
        a.addButton(withTitle: "好")
        a.runModal()
    }

    @objc func copyHA() {
        refreshLanIP()
        let text = "主机：\(lanIP)\n端口：\(port)\n令牌：\(token)\nURL：http://\(lanIP):\(port)/status.json"
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        let a = NSAlert()
        a.messageText = "已复制 HA 配置"
        a.informativeText = text
        a.addButton(withTitle: "好")
        a.runModal()
    }

    @objc func restartWorker() {
        supervisor.restart()
        consecutivePollFail = 0
        poll()
        let a = NSAlert()
        a.messageText = "重启数据服务"
        a.informativeText = "工作进程已重启。"
        a.addButton(withTitle: "好")
        a.runModal()
        poll()
    }

    @objc func openDir() {
        NSWorkspace.shared.open(URL(fileURLWithPath: workDir))
    }

    func applicationWillTerminate(_ note: Notification) {
        supervisor.stop()
        try? FileManager.default.removeItem(atPath: lockPath)
    }
}

struct CliResult {
    let ok: Bool
    let text: String
    var displayText: String { text.isEmpty ? "完成" : text }
}

/// python 采集工作进程监管器：无独立生命周期的内部子进程。
/// worker 侧另有父进程看门狗（App 死亡 → worker 5s 内自退），双保险不留孤儿。
final class WorkerSupervisor {
    private(set) var process: Process?
    private(set) var lastSpawnDate = Date.distantPast
    private var outHandle: FileHandle?
    private var errHandle: FileHandle?
    private let workerPath = workDir + "/exporter.py"
    private let configPath = workDir + "/config.json"

    var isRunning: Bool { process?.isRunning ?? false }

    init() {
        // worker 的输出接回日志文件（恢复"看 exporter.err.log 排障"的契约）
        for name in ["exporter.log", "exporter.err.log"] {
            let path = workDir + "/" + name
            if !FileManager.default.fileExists(atPath: path) {
                FileManager.default.createFile(atPath: path, contents: nil)
            }
        }
        outHandle = FileHandle(forWritingAtPath: workDir + "/exporter.log")
        errHandle = FileHandle(forWritingAtPath: workDir + "/exporter.err.log")
        outHandle?.seekToEndOfFile()
        errHandle?.seekToEndOfFile()
    }

    /// 兜底清理：launchd kickstart/上次异常退出可能留下孤儿工作进程（占 8787）
    func killOrphans() {
        let p = Process()
        p.launchPath = "/usr/bin/pkill"
        p.arguments = ["-f", "mc-ha-exporter/exporter.py"]
        p.standardOutput = Pipe()
        p.standardError = Pipe()
        guard (try? p.run()) != nil else { return }   // 未 launch 就 wait 会崩 App
        p.waitUntilExit()
    }

    func ensure() {
        guard !isRunning else { return }
        guard Date().timeIntervalSince(lastSpawnDate) > 8 else { return }   // 重生节流
        spawn()
    }

    func restart() {
        stop()
        spawn()
    }

    func stop() {
        if let p = process, p.isRunning {
            p.terminate()
            // 等 worker 真正退出（释放 8787），最多 5s，超时强杀
            let deadline = Date().addingTimeInterval(5)
            while p.isRunning && Date() < deadline {
                usleep(100_000)
            }
            if p.isRunning {
                kill(pid_t(p.processIdentifier), SIGKILL)
            }
        }
        process = nil
    }

    private func spawn() {
        lastSpawnDate = Date()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        p.arguments = [workerPath, configPath]
        // 输出接日志文件而非无读取端的 Pipe：管道写满会把 worker 卡死在 write 上
        if let out = outHandle { p.standardOutput = out }
        if let err = errHandle { p.standardError = err }
        p.terminationHandler = { [weak self] finished in
            DispatchQueue.main.async {
                if self?.process === finished { self?.process = nil }
            }
        }
        do { try p.run(); process = p } catch { process = nil }
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()

#if os(Linux)
import Foundation
import Glibc
import BotCore

@main struct LinuxMain {
    @MainActor static func main() async {
        setbuf(stdout, nil)
        umask(0o077)
        do {
            if CommandLine.arguments.contains("--xiaohongshu-panel") || CommandLine.arguments.contains("--social-panel") {
                let workspace = SocialPlatforms()
                let server = try workspace.panel(); print("SOCIAL_CONTROL_URL=\(server.controlURL.absoluteString)"); print("XHS_CONTROL_URL=\(server.controlURL.absoluteString)")
                defer { withExtendedLifetime(server) {} }
                while !Task.isCancelled { try await Task.sleep(nanoseconds: 60_000_000_000) }
                return
            }
            if let index = CommandLine.arguments.firstIndex(of: "--qq-check") {
                guard CommandLine.arguments.indices.contains(index + 2) else {
                    throw AppFailure.message("用法：--qq-check 本机端点 预期账号；令牌由标准输入提供")
                }
                var config = QQConfig()
                config.endpoint = CommandLine.arguments[index + 1]
                config.expectedSelfID = CommandLine.arguments[index + 2]
                try config.validate()
                let connection = OneBotConnection()
                defer { connection.close() }
                try await connection.connect(endpoint: config.endpoint, token: readLine() ?? "")
                async let login = connection.action("get_login_info")
                async let health = connection.action("get_status")
                let (a, b) = try await (login, health)
                guard let account = a["data"] as? [String: Any], QQPolicy.identifier(account["user_id"]) == config.expectedSelfID,
                      let status = b["data"] as? [String: Any], status["online"] as? Bool == true, status["good"] as? Bool == true else {
                    throw AppFailure.message("QQ 账号或在线状态未通过核对")
                }
                print("QQ 账号与在线状态核对成功；未读取聊天或发送消息")
                return
            }
            // 默认 QQ 入口：QQEngine + QQControlServer（旧行为完全不变）。
            let engine = QQEngine(allowAuthenticationUI: false)
            let server = try QQControlServer(engine: engine)
            server.start()
            print("Windows / Linux 本机面板：凭证仅保存在内存，退出后需重新填写。")
            // Keep the engine and listener alive without blocking MainActor.
            defer { withExtendedLifetime(server) {} }

            // 单进程统一回复试点：显式开关打开时，在**同一个 PID / 同一个 QQEngine /
            // 同一个 MainActor** 上再起一个独立本机监听。两个 LoopbackListener 各拿
            // 随机端口，QQ 控制面板 /api/status 与 15s 连接超时不受影响。
            // 开关不开时下面整段不执行，旧启动方式、现有配置默认行为逐字不变。
            // ⚠️ pilot 必须在函数作用域持有：若只在 if 块内声明，块结束即释放，
            //    LoopbackListener.deinit 会关掉监听端口（实测只剩 QQ 面板端口存活）。
            var pilot: UnifiedPilotServer? = nil
            defer { withExtendedLifetime(pilot) {} }
            let pilotConfig = UnifiedCoreConfig.fromEnvironment()
            if pilotConfig.enabled {
                let env = ProcessInfo.processInfo.environment
                let fixtureBase: URL? = {
                    guard let raw = env["DAFEIYU_UNIFIED_MODEL_BASE_URL"], !raw.isEmpty,
                          let url = URL(string: raw), ["http", "https"].contains(url.scheme?.lowercased() ?? "") else { return nil }
                    return url
                }()
                let provider = QQEngineSharedStateProvider(engine: engine,
                                                           fixtureModelKey: env["DAFEIYU_UNIFIED_MODEL_KEY"] ?? "")
                let generator = provider.makeGenerator(fixtureBaseURL: fixtureBase,
                                                       fixtureModel: env["DAFEIYU_UNIFIED_MODEL"])
                let core = UnifiedReplyCore(config: pilotConfig,
                                            generator: generator,
                                            sharedState: provider)
                let instance = try UnifiedPilotServer(core: core)
                pilot = instance
                instance.start()
                print("统一回复试点已在同一 QQ 进程内启用：共享实时模型配置/人格/第五人格索引，按平台隔离会话。")
            }

            while !Task.isCancelled { try await Task.sleep(nanoseconds: 60_000_000_000) }
        } catch { print("启动失败：\(error.localizedDescription)"); exit(1) }
    }
}
#endif

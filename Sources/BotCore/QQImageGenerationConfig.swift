import Foundation

public enum QQImageProvider: String, Codable, CaseIterable, Sendable {
    case zhipu, cloudflare, sensenova
    public var title: String {
        switch self {
        case .zhipu: return "智谱 CogView-3-Flash"
        case .cloudflare: return "Cloudflare FLUX.1 schnell"
        case .sensenova: return "商汤 SenseNova U1"
        }
    }
    public var alternate: QQImageProvider { self == .zhipu ? .cloudflare : .zhipu }
}

// Credentials are deliberately excluded from Codable settings.
public struct QQImageGenerationConfig: Codable, Equatable, Sendable {
    public var enabled = false
    public var primary: QQImageProvider = .zhipu
    public var fallbackEnabled = true
    public var cloudflareAccountID = ""
    public var sensenovaModel: String?
    public var effectiveSenseNovaModel: String { sensenovaModel ?? "sensenova-u1-fast" }
    public init() {}
    public func validate() throws {
        guard effectiveSenseNovaModel.range(of: "^[a-zA-Z0-9._-]{1,128}$", options: .regularExpression) != nil else {
            throw CoreError.invalid("商汤生图模型 ID 格式无效")
        }
        guard cloudflareAccountID.isEmpty || cloudflareAccountID.range(of: "^[a-fA-F0-9]{32}$", options: .regularExpression) != nil else {
            throw CoreError.invalid("Cloudflare Account ID 应为 32 位十六进制字符（不是 API Token）")
        }
    }
}

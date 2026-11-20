import Foundation

/// Conservative, local-only redaction. It never calls a network service.
/// Order matters: national ID and phone are handled before looser numeric patterns.
public enum QQMemoryRedactor {
    public static func redact(_ text: String) -> String {
        var result = QQMemoryBook.redact(text)
        result = replace(#"(?<!\d)\d{17}[\dXx](?!\d)"#, in: result, with: "[证件号省略]")
        result = replace(#"(?<!\d)1[3-9]\d{9}(?!\d)"#, in: result, with: "[手机号省略]")
        result = replace(#"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"#, in: result, with: "[邮箱省略]")
        result = replace(#"(?i)(QQ号|QQ|账号|群号)\s*[:：]?\s*\d{5,12}"#, in: result, with: "$1[号码省略]")
        return result
    }

    public static func containsSensitive(_ text: String) -> Bool {
        redact(text) != text
    }

    private static func replace(_ pattern: String, in text: String, with template: String) -> String {
        guard let expression = try? NSRegularExpression(pattern: pattern) else { return text }
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        return expression.stringByReplacingMatches(in: text, range: range, withTemplate: template)
    }
}
